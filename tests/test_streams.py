import pytest

from fastapi_prod_hardening.bounded_llm import (
    AllProvidersFailed,
    BoundedLLM,
    FakeProvider,
)


async def collect(llm, **kw):
    return [t async for t in llm.astream("hi", **kw)]


async def test_stream_uses_first_healthy_provider():
    bad, good = FakeProvider("bad", fail_times=1), FakeProvider("good", tokens=("a", "b"))
    assert await collect(BoundedLLM([bad, good])) == ["a", "b"]
    assert bad.calls == 1 and good.calls == 1


async def test_stream_does_not_fall_back_after_first_token():
    class DiesMidStream(FakeProvider):
        async def astream(self, prompt):
            yield "partial"
            raise RuntimeError("dropped")

    backup = FakeProvider("backup")
    got = []
    with pytest.raises(RuntimeError):
        async for tok in BoundedLLM([DiesMidStream(), backup]).astream("hi"):
            got.append(tok)
    assert got == ["partial"] and backup.calls == 0  # no splicing a second answer on the first


async def test_stream_all_providers_failing_raises():
    with pytest.raises(AllProvidersFailed):
        await collect(BoundedLLM([FakeProvider(fail_times=1), FakeProvider(fail_times=1)]))
