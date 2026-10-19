"""Lightweight in-process metrics: event-loop lag and DB pool wait. No dependencies."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from bisect import bisect_left

BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5)


class Histogram:
    """Cumulative buckets in the Prometheus text format, so a real scraper can read /metrics."""

    def __init__(self, name: str, help_text: str, buckets: tuple[float, ...] = BUCKETS) -> None:
        self.name, self.help, self.buckets = name, help_text, buckets
        self._counts = [0] * (len(buckets) + 1)
        self.sum = 0.0
        self._lock = threading.Lock()  # observed from worker threads (pool waits)

    def observe(self, value: float) -> None:
        with self._lock:
            self._counts[bisect_left(self.buckets, value)] += 1
            self.sum += value

    @property
    def count(self) -> int:
        return sum(self._counts)

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        running = 0
        for bound, n in zip(self.buckets, self._counts):
            running += n
            lines.append(f'{self.name}_bucket{{le="{bound}"}} {running}')
        lines += [f'{self.name}_bucket{{le="+Inf"}} {self.count}',
                  f"{self.name}_sum {self.sum}", f"{self.name}_count {self.count}"]
        return "\n".join(lines)


class LoopLagSampler:
    """Sleeps a fixed interval and records how late it wakes.

    If a handler blocks the loop, this task wakes late by about that long, so the overshoot is a
    direct measure of how long the loop was unavailable to every other request.
    """

    def __init__(self, interval_s: float = 0.05) -> None:
        self.interval_s = interval_s
        self.histogram = Histogram("event_loop_lag_seconds", "Event loop wake-up delay")
        self.last = self.max = 0.0
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            start = loop.time()
            await asyncio.sleep(self.interval_s)
            self.last = max(0.0, loop.time() - start - self.interval_s)
            self.max = max(self.max, self.last)
            self.histogram.observe(self.last)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class Metrics:
    def __init__(self, lag_interval_s: float = 0.05) -> None:
        self.loop_lag = LoopLagSampler(lag_interval_s)
        self.pool_wait = Histogram("db_pool_wait_seconds", "Time spent waiting for a DB connection")

    def render(self) -> str:
        return "\n".join([self.loop_lag.histogram.render(), self.pool_wait.render()]) + "\n"
