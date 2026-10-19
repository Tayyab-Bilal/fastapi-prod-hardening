# Usage guide

Every snippet here is executed by `tests/test_docs.py`, so it runs as written. All of it works
offline: no LLM provider, no Redis server, no network.

## The example app

`create_app` wires every piece together into a small "Acme Workspace" API.

```python
from fastapi.testclient import TestClient

from fastapi_prod_hardening.app import create_app
from fastapi_prod_hardening.identity import Hs256Verifier, Identity

token = Hs256Verifier("dev-secret").issue(Identity("acme", "alice"))
with TestClient(create_app(secret="dev-secret")) as client:
    headers = {"authorization": f"Bearer {token}"}
    print(client.post("/v1/tasks", json={"title": "Plan sprint", "tenant_id": "someone-else"},
                      headers=headers).json())
    print(client.get("/v1/tasks/count", headers=headers).json())
    assert "event_loop_lag_seconds_count" in client.get("/metrics").text
```

The body says tenant `someone-else`; the task is saved for `acme`, because identity comes from the
token. `mode="enforce"` would reject the mismatch with a 403; the default `mode="measure"` only logs it.

Routes in the app show the event-loop rules:

- `GET /v1/tasks/count` is a plain `def`. It only touches the database, so FastAPI runs it on the
  thread pool and the event loop stays free.
- `POST /v1/tasks` is `async def` because a real handler would also await other things. Its database
  block is offloaded with `anyio.to_thread.run_sync`.
- Both draw from one thread budget, sized to the database pool (`pool_size`, default 30).

## Rate limiting

Buckets are per caller. `MemoryBackend` is per process; `RedisBackend` is shared.

```python
import asyncio

from fastapi_prod_hardening.ratelimit import MemoryBackend, RateLimiter


async def main() -> None:
    limiter = RateLimiter(MemoryBackend())
    results = [(await limiter.hit("user:alice", 3)).allowed for _ in range(5)]
    print(results)  # three allowed, then blocked
    assert results == [True, True, True, False, False]


asyncio.run(main())
```

To use the middleware yourself, add `RateLimitMiddleware(limiter=..., settings=CallerSettings(...))`
(see `create_app`). Add the CORS middleware last, so it is outermost and 429s still carry CORS headers.

With Redis, swap the backend. The counter is one Lua script, with a 250 ms deadline and a 30 s
cooldown if Redis fails (requests are then allowed, not stalled):

```python
import asyncio

import fakeredis.aioredis

from fastapi_prod_hardening.ratelimit import RateLimiter, RedisBackend


async def main() -> None:
    limiter = RateLimiter(RedisBackend(fakeredis.aioredis.FakeRedis()))
    allowed = sum([(await limiter.hit("job:worker", 10)).allowed for _ in range(12)])
    assert allowed == 10


asyncio.run(main())
```

## Bounded LLM calls

```python
import asyncio

from fastapi_prod_hardening.bounded_llm import BoundedLLM, FakeProvider


async def main() -> None:
    llm = BoundedLLM([FakeProvider("primary", fail_times=1), FakeProvider("backup")],
                     call_deadline_s=240, max_concurrency=50)
    print(await llm.complete("summarise my tasks"))  # primary fails, backup answers
    tokens = [t async for t in llm.astream("hi", caller_facing=True)]  # 270 s cap
    print(tokens)


asyncio.run(main())
```

`complete` tries each provider once, in order, and stops trying when the 240 s deadline is used up.
`astream` picks the first healthy provider and does not switch after the first token. Pass
`caller_facing=True` for a stream a person is watching (270 s cap); background streams get a
900 s backstop. When the breaker is open, calls fail at once without sending a request.

```python
import asyncio

from fastapi_prod_hardening.bounded_llm import (
    AllProvidersFailed, BoundedLLM, CircuitBreaker, CircuitOpen, FakeProvider)


async def main() -> None:
    dead = FakeProvider(fail_times=99)
    llm = BoundedLLM([dead], breaker=CircuitBreaker(failure_threshold=2))
    for _ in range(2):
        try:
            await llm.complete("hi")
        except AllProvidersFailed:
            pass
    try:
        await llm.complete("hi")
    except CircuitOpen:
        print("fast fail; provider calls:", dead.calls)
    assert dead.calls == 2


asyncio.run(main())
```

To use a real provider, implement the `Provider` protocol (`complete` and `astream`).

## Shared HTTP client

```python
import asyncio

import httpx

from fastapi_prod_hardening.http import HttpClients


async def main() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tenant": request.headers["x-tenant-id"]})

    clients = HttpClients(transport=httpx.MockTransport(handler))  # omit transport in real use
    first = await clients.request("GET", "http://svc.test/", tenant_id="acme", token="tok")
    second = await clients.request("GET", "http://svc.test/", tenant_id="globex")
    print(first.json(), second.json())
    assert "x-tenant-id" not in clients.get().headers
    await clients.aclose()


asyncio.run(main())
```

In an app, create `HttpClients()` once, pass it to `create_app(http=...)`, and the lifespan closes
it on shutdown. Never put a tenant id or token in `default headers`: the client is shared.
HTTP/2 is on when the `h2` package is installed (it comes with `httpx[http2]`).

## Keeping large payloads out of the broker

```python
import logging
import tempfile
from pathlib import Path

from fastapi_prod_hardening.transcripts import TranscriptStore, log_task_start

store = TranscriptStore(str(Path(tempfile.mkdtemp()) / "t.db"))
store.put("job-1", "long meeting transcript")
message = {"job_id": "job-1"}  # this is all the task message carries


def summarise(job_id: str) -> str:
    log_task_start(logging.getLogger("worker"), summarise, job_id)  # logs "job_id", not its value
    return store.take(job_id) or ""  # returns the text and deletes it


assert summarise(message["job_id"]) == "long meeting transcript"
assert store.count() == 0
print("swept:", store.sweep())  # run daily: removes rows older than 24 h
```

`put` raises `PayloadTooLarge` over 50 MB. If the worker is killed before `take`, the 24 h sweep
removes the row.

## Redaction

```python
import io
import logging

from fastapi_prod_hardening.redaction import install_redaction

stream = io.StringIO()
root = logging.getLogger("usage-root")
root.propagate, root.level = False, logging.INFO
root.addHandler(logging.StreamHandler(stream))
install_redaction(root, secrets=["my-api-secret"])
logging.getLogger("usage-root.billing").info("dsn postgresql://app:hunter2@db/acme my-api-secret")
print(stream.getvalue().strip())
assert "hunter2" not in stream.getvalue() and "my-api-secret" not in stream.getvalue()
```

Call `install_redaction()` again after a framework (a task runner, say) replaces the handlers.

## CORS allow-list from data

```python
import asyncio
import tempfile
from pathlib import Path

from fastapi_prod_hardening.cors import DynamicCORS, SQLiteOriginSource


async def main() -> None:
    source = SQLiteOriginSource(str(Path(tempfile.mkdtemp()) / "o.db"))
    source.add("https://app.acme.example")
    try:
        source.add("*")
    except ValueError as exc:
        print("rejected:", exc)
    cors = DynamicCORS(source, interval_s=60)
    await cors.start()  # loads once, then reloads every 60 s in the background
    assert cors.origins == {"https://app.acme.example"}
    await cors.stop()


asyncio.run(main())
```

With no rows in the table, no origin is allowed: CORS is inert until you add some.

## Event-loop hygiene, metrics and atomic counters

```python
import asyncio
import tempfile
import time
from pathlib import Path

import anyio.to_thread

from fastapi_prod_hardening import counters
from fastapi_prod_hardening.db import Pool
from fastapi_prod_hardening.metrics import Metrics
from fastapi_prod_hardening.threads import shared_limiter


async def main() -> None:
    metrics = Metrics(lag_interval_s=0.01)
    pool = Pool(str(Path(tempfile.mkdtemp()) / "app.db"), size=5, wait_histogram=metrics.pool_wait)
    with pool.connection() as conn:
        counters.init(conn)
    shared_limiter(5)  # same number as the pool
    metrics.loop_lag.start()

    def db_block() -> int:  # runs on a worker thread, so it may block
        with pool.connection() as conn:
            return counters.increment(conn, "tasks")

    numbers = await asyncio.gather(*(anyio.to_thread.run_sync(db_block) for _ in range(20)))
    assert sorted(numbers) == list(range(1, 21))  # no lost updates
    time.sleep(0.1)  # a blocking call in async code: the lag sampler notices
    await asyncio.sleep(0.02)
    await metrics.loop_lag.stop()
    print(metrics.render().splitlines()[0])
    assert metrics.loop_lag.max >= 0.05


asyncio.run(main())
```

`GET /metrics` serves the same text in the Prometheus exposition format.

## Load test with a /health canary

Not part of the test suite. Run the app with the debug routes, then point the harness at a route:

```bash
uvicorn --factory fastapi_prod_hardening.app:load_test_app &
python scripts/loadtest.py http://127.0.0.1:8000 /debug/blocks-loop   # async def + time.sleep
python scripts/loadtest.py http://127.0.0.1:8000 /debug/threadpool    # same work as a sync def
```

Watch `canary_p50_ms` and `canary_max_ms`. If `/health` slows down while the other route is busy,
something is blocking the event loop.

## CI gates

```bash
pytest -q --strict-skips --junitxml=report.xml   # any skipped test fails the run
python scripts/check_junit.py report.xml          # fails on missing, empty or all-skipped report
python scripts/check_junit.py report.xml --max-skips 0
python scripts/check_migrations.py path/to/migrations
```

`--strict-skips` comes from the `fastapi_prod_hardening.strict_skips` plugin (load it with
`-p fastapi_prod_hardening.strict_skips`; this repo does it in `pyproject.toml`).
`check_migrations.py` reads `revision` and `down_revision` with `ast`, so it needs no app
dependencies. It reports duplicate ids, missing parents, cycles and multiple heads.

```python
import subprocess
import sys

done = subprocess.run([sys.executable, "scripts/check_migrations.py", "tests/fixtures/multihead"],
                      capture_output=True, text=True)
print(done.stderr.strip())
assert done.returncode == 1 and "multiple heads" in done.stderr
```

The workflow templates are in `.github/workflows/` and explained in [change-aware-ci.md](change-aware-ci.md).
