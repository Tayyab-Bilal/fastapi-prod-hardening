"""Every LLM call is bounded: concurrency, whole-call deadline, stream cap, circuit breaker."""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import AsyncIterator, Callable, Protocol


class LLMError(Exception):
    pass


class CircuitOpen(LLMError):
    pass


class DeadlineExceeded(LLMError):
    pass


class AllProvidersFailed(LLMError):
    pass


class StreamCapExceeded(LLMError):
    pass


class Provider(Protocol):
    async def complete(self, prompt: str) -> str: ...
    def astream(self, prompt: str) -> AsyncIterator[str]: ...


class FakeProvider:
    """Scripted provider for tests and the demo: fixed latency, first ``fail_times`` calls fail."""

    def __init__(self, name: str = "fake", latency: float = 0.0, fail_times: int = 0,
                 tokens: tuple[str, ...] = ("hello", " ", "world")) -> None:
        self.name, self.latency, self.fail_times, self.tokens = name, latency, fail_times, tokens
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.latency)
            if self.calls <= self.fail_times:
                raise RuntimeError(f"{self.name} scripted failure")
            return "".join(self.tokens)
        finally:
            self.in_flight -= 1

    async def astream(self, prompt: str) -> AsyncIterator[str]:
        """Scripted stream; fails before the first token while ``calls <= fail_times``."""
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"{self.name} scripted failure")
        for tok in self.tokens:
            await asyncio.sleep(self.latency)
            yield tok


class CircuitBreaker:
    """closed -> open after N failures -> half-open after cool-down -> closed on one success."""

    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 30,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.cooldown_s, self.clock = failure_threshold, cooldown_s, clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self.clock() - self.opened_at >= self.cooldown_s else "open"

    def before_call(self) -> None:
        if self.state == "open":
            raise CircuitOpen("circuit open")

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half-open" or self.failures >= self.threshold:
            self.opened_at = self.clock()


class BoundedLLM:
    """Wraps a provider list with the limits below.

    Builds on per-attempt timeouts and a circuit breaker. Adds the concurrency cap,
    the whole-call deadline, stream caps and the call-time breaker check.
    """

    def __init__(self, providers: list[Provider], *, max_concurrency: int = 50,
                 call_deadline_s: float = 240, per_attempt_timeout_s: float = 60,
                 stream_cap_s: float = 270, background_stream_cap_s: float = 900,
                 breaker: CircuitBreaker | None = None) -> None:
        self.providers = providers
        self.max_concurrency = max_concurrency
        self.call_deadline_s = call_deadline_s
        self.per_attempt_timeout_s = per_attempt_timeout_s
        self.stream_cap_s = stream_cap_s
        self.background_stream_cap_s = background_stream_cap_s
        self.breaker = breaker or CircuitBreaker()
        # A semaphore binds to the loop that first contends on it. A client shared across repeated
        # asyncio.run() calls would crash on the second loop, so keep one per running loop.
        self._sems: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary())

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if loop not in self._sems:
            self._sems[loop] = asyncio.Semaphore(self.max_concurrency)
        return self._sems[loop]

    async def complete(self, prompt: str) -> str:
        async with self._semaphore():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.call_deadline_s
            last: Exception | None = None
            for provider in self.providers:  # each provider is tried once: fallbacks never retry
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise DeadlineExceeded(f"{self.call_deadline_s}s call deadline") from last
                self.breaker.before_call()
                try:
                    result = await asyncio.wait_for(
                        provider.complete(prompt), min(self.per_attempt_timeout_s, remaining))
                except Exception as exc:
                    self.breaker.record_failure()
                    last = exc
                    continue
                self.breaker.record_success()
                return result
            raise AllProvidersFailed("all providers failed") from last


    async def astream(self, prompt: str, *, caller_facing: bool = False) -> AsyncIterator[str]:
        """Stream from the first healthy provider; there is no fallback once tokens have flowed.

        A provider that fails before its first token is skipped and the next one is tried. After the
        first token the caller has already seen output, so switching provider would splice two
        answers together. The stream cap applies only to caller-facing streams: background work
        gets a longer backstop so it is not cut off under a limit meant for a waiting person.
        """
        cap = self.stream_cap_s if caller_facing else self.background_stream_cap_s
        async with self._semaphore():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + cap
            last: Exception | None = None
            for provider in self.providers:
                self.breaker.before_call()
                stream = provider.astream(prompt).__aiter__()
                started = False
                try:
                    while True:
                        try:
                            token = await asyncio.wait_for(
                                stream.__anext__(), max(0, deadline - loop.time()))
                        except StopAsyncIteration:
                            self.breaker.record_success()
                            return
                        except asyncio.TimeoutError as exc:
                            self.breaker.record_failure()
                            raise StreamCapExceeded(f"stream exceeded {cap}s") from exc
                        except Exception as exc:
                            self.breaker.record_failure()
                            if started:
                                raise  # Simplification: no mid-stream fallback; see docstring
                            last = exc
                            break  # failed before the first token: try the next provider
                        started = True
                        yield token
                finally:
                    await stream.aclose()  # type: ignore[attr-defined]
            raise AllProvidersFailed("all providers failed") from last
