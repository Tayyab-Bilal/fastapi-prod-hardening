import asyncio

import pytest
from conftest import Clock

from fastapi_prod_hardening.bounded_llm import (
    AllProvidersFailed,
    BoundedLLM,
    CircuitBreaker,
    CircuitOpen,
    DeadlineExceeded,
    FakeProvider,
    StreamCapExceeded,
)


async def test_concurrency_cap():
    p = FakeProvider(latency=0.02)
    llm = BoundedLLM([p], max_concurrency=3)
    await asyncio.gather(*(llm.complete("q") for _ in range(12)))
    assert p.max_in_flight == 3


async def test_deadline_stops_new_fallbacks():
    slow = [FakeProvider(f"p{i}", latency=1.0) for i in range(4)]
    llm = BoundedLLM(slow, call_deadline_s=0.1, per_attempt_timeout_s=0.06,
                     breaker=CircuitBreaker(failure_threshold=99))
    with pytest.raises(DeadlineExceeded):
        await llm.complete("q")
    assert [p.calls for p in slow] == [1, 1, 0, 0]  # third attempt never starts


async def test_fallbacks_do_not_retry():
    ps = [FakeProvider(f"p{i}", fail_times=99) for i in range(3)]
    llm = BoundedLLM(ps, breaker=CircuitBreaker(failure_threshold=99))
    with pytest.raises(AllProvidersFailed):
        await llm.complete("q")
    assert [p.calls for p in ps] == [1, 1, 1]
    assert await BoundedLLM([FakeProvider(fail_times=1), FakeProvider("b")]).complete("q")


async def test_open_circuit_fails_fast_with_zero_calls():
    p = FakeProvider(fail_times=99)
    llm = BoundedLLM([p], breaker=CircuitBreaker(failure_threshold=2, clock=Clock()))
    for _ in range(2):
        with pytest.raises(AllProvidersFailed):
            await llm.complete("q")
    before = p.calls
    with pytest.raises(CircuitOpen):
        await llm.complete("q")
    assert p.calls == before


async def test_half_open_recovers():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=30, clock=clock)
    p = FakeProvider(fail_times=1)
    llm = BoundedLLM([p], breaker=breaker)
    with pytest.raises(AllProvidersFailed):
        await llm.complete("q")
    assert breaker.state == "open"
    clock.now += 31
    assert breaker.state == "half-open"
    assert await llm.complete("q") == "hello world"
    assert breaker.state == "closed"


async def test_stream_cap_only_for_caller_facing():
    p = FakeProvider(latency=0.03, tokens=tuple("abcdefghij"))
    llm = BoundedLLM([p], stream_cap_s=0.08, background_stream_cap_s=5)
    with pytest.raises(StreamCapExceeded):
        [t async for t in llm.astream("q", caller_facing=True)]
    assert len([t async for t in llm.astream("q", caller_facing=False)]) == 10


def test_separate_state_per_event_loop():
    llm = BoundedLLM([FakeProvider(latency=0.01)], max_concurrency=1)
    sems = []

    async def burst():
        sems.append(llm._semaphore())
        await asyncio.gather(*(llm.complete("q") for _ in range(3)))  # contention binds the loop

    asyncio.run(burst())
    asyncio.run(burst())  # a single shared semaphore raises "bound to a different event loop"
    assert sems[0] is not sems[1]
