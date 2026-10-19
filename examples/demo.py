"""Walkthrough of each hardening piece with fakes only: no network, no real LLM, no Redis server."""

import asyncio
import io
import logging
import tempfile
import threading
import time
from pathlib import Path

import fakeredis.aioredis
import httpx
from fastapi.testclient import TestClient

from fastapi_prod_hardening import counters
from fastapi_prod_hardening.app import create_app
from fastapi_prod_hardening.bounded_llm import (
    AllProvidersFailed,
    BoundedLLM,
    CircuitBreaker,
    CircuitOpen,
    DeadlineExceeded,
    FakeProvider,
)
from fastapi_prod_hardening.cors import DynamicCORS, SQLiteOriginSource
from fastapi_prod_hardening.db import Pool
from fastapi_prod_hardening.http import HttpClients
from fastapi_prod_hardening.identity import Hs256Verifier, Identity
from fastapi_prod_hardening.metrics import LoopLagSampler
from fastapi_prod_hardening.ratelimit import RateLimiter, RedisBackend
from fastapi_prod_hardening.redaction import install_redaction
from fastapi_prod_hardening.threads import shared_limiter
from fastapi_prod_hardening.transcripts import TranscriptStore, log_task_start


def title(text: str) -> None:
    print(f"\n== {text}")


def rate_limiting_and_identity() -> None:
    title("1. Rate limit per caller + identity from the token")
    tmp = Path(tempfile.mkdtemp())
    src = SQLiteOriginSource(str(tmp / "origins.db"))
    src.add("https://app.acme.example")
    token = Hs256Verifier("dev-secret").issue(Identity("acme", "alice"))
    with TestClient(create_app(origin_source=src, mode="measure")) as c:
        ip = [c.get("/v1/whoami").status_code for _ in range(101)]
        print(f"anonymous IP bucket: 100 ok, request 101 -> {ip[-1]}")
        r = c.get("/v1/whoami", headers={"origin": "https://app.acme.example"})
        print(f"429 carries CORS: {r.headers.get('access-control-allow-origin')}, "
              f"Retry-After={r.headers.get('retry-after')}s")
        body = {"title": "Plan sprint", "tenant_id": "someone-else"}
        r = c.post("/v1/tasks", json=body, headers={"authorization": f"Bearer {token}",
                                                    "x-service-key": "demo-service-key"})
        print(f"body says tenant 'someone-else', token says 'acme' -> saved for: {r.json()['tenant_id']}")
    with TestClient(create_app(mode="enforce")) as c:
        r = c.post("/v1/tasks", json=body, headers={"authorization": f"Bearer {token}"})
        print(f"enforce mode rejects the mismatch -> {r.status_code}")


async def redis_failsafe() -> None:
    title("2. Redis: atomic Lua counter, fail open on error")
    limiter = RateLimiter(RedisBackend(fakeredis.aioredis.FakeRedis()))
    allowed = sum([(await limiter.hit("user:alice", 100)).allowed for _ in range(120)])
    print(f"120 hits, limit 100 -> {allowed} allowed")

    class Down:
        async def incr(self, key, window_s):
            raise ConnectionError("redis down")

    limiter = RateLimiter(Down())
    print("redis down -> allowed:", (await limiter.hit("k", 1)).allowed,
          "| in cooldown, next hit skips redis:", (await limiter.hit("k", 1)).allowed)


def redaction() -> None:
    title("3. Redaction at the handler")
    stream = io.StringIO()
    root = logging.getLogger("demo-root")
    root.propagate, root.level = False, logging.INFO
    root.addHandler(logging.StreamHandler(stream))
    install_redaction(root)
    logging.getLogger("demo-root.billing").info(
        "connect postgresql://app:hunter2@db/acme key=sk-live1234567890abcdef Bearer abc.def.ghi")
    print(stream.getvalue().strip())


async def bounded_llm() -> None:
    title("4. Bounded LLM calls")
    llm = BoundedLLM([FakeProvider("primary", fail_times=1), FakeProvider("backup")])
    print("primary fails, backup answers:", await llm.complete("hi"))

    slow = [FakeProvider(f"p{i}", latency=1) for i in range(4)]
    llm = BoundedLLM(slow, call_deadline_s=0.1, per_attempt_timeout_s=0.06,
                     breaker=CircuitBreaker(failure_threshold=99))
    try:
        await llm.complete("hi")
    except DeadlineExceeded:
        print("deadline hit; provider calls per fallback:", [p.calls for p in slow])

    bad = FakeProvider(fail_times=99)
    llm = BoundedLLM([bad], breaker=CircuitBreaker(failure_threshold=2))
    for _ in range(3):
        try:
            await llm.complete("hi")
        except (AllProvidersFailed, CircuitOpen) as exc:
            print(f"  {type(exc).__name__} (provider calls so far: {bad.calls})")


async def cors_reload() -> None:
    title("5. CORS allow-list reload keeps last good list")
    src = SQLiteOriginSource(str(Path(tempfile.mkdtemp()) / "o.db"))
    src.add("https://app.acme.example")
    cors = DynamicCORS(src)
    await cors.refresh()
    src.load = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    await cors.refresh()
    print("after failed reload:", sorted(cors.origins))
    for bad in ("*", "https://x.example/path", "http://x.example"):
        try:
            SQLiteOriginSource(str(Path(tempfile.mkdtemp()) / "o.db")).add(bad)
        except ValueError as exc:
            print(f"  admin rejects {bad!r}: {exc}")


async def threads() -> None:
    title("6. One thread budget")
    print("limiter total tokens:", shared_limiter(30).total_tokens)


async def shared_http_client() -> None:
    title("7. Shared HTTP client: tenant headers per request, never on the client")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    clients = HttpClients(transport=httpx.MockTransport(handler))
    await clients.request("GET", "http://svc.test/", tenant_id="acme", token="tok-a")
    await clients.request("GET", "http://svc.test/", tenant_id="globex")
    print("http2 enabled:", clients.http2)
    print("request 1 tenant:", seen[0].headers["x-tenant-id"],
          "| request 2 tenant:", seen[1].headers["x-tenant-id"],
          "| request 2 has auth:", "authorization" in seen[1].headers)
    print("tenant header stored on client:", "x-tenant-id" in clients.get().headers)
    await clients.aclose()


def broker_minimisation() -> None:
    title("8. Transcript by job id, deleted after delivery")
    clock = [1000.0]
    store = TranscriptStore(str(Path(tempfile.mkdtemp()) / "t.db"), clock=lambda: clock[0])
    store.put("job-1", "very long meeting transcript")
    message = {"job_id": "job-1"}
    print("broker message:", message)

    def summarise(job_id, transcript):
        ...

    log_task_start(logging.getLogger("demo-task"), summarise, "job-1", "very long meeting transcript")
    print("worker took:", store.take(message["job_id"]), "| rows left:", store.count())
    store.put("job-2", "worker was killed before taking this")
    clock[0] += 25 * 3600
    print("24h sweep removed:", store.sweep(), "row(s)")


def atomic_counter() -> None:
    title("9. Atomic counter: 8 threads x 100 increments")
    pool = Pool(str(Path(tempfile.mkdtemp()) / "c.db"), size=8)
    with pool.connection() as conn:
        counters.init(conn)

    def work() -> None:
        for _ in range(100):
            with pool.connection() as conn:
                counters.increment(conn, "n")

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with pool.connection() as conn:
        print("recorded:", counters.get(conn, "n"), "of 800")


async def loop_lag() -> None:
    title("10. Event-loop lag sampler sees a blocking handler")
    sampler = LoopLagSampler(interval_s=0.01)
    sampler.start()
    await asyncio.sleep(0.05)
    time.sleep(0.2)  # what a sync DB call inside `async def` does to the loop
    await asyncio.sleep(0.05)
    await sampler.stop()
    print(f"worst wake-up delay: {sampler.max * 1000:.0f} ms (a healthy loop: a few ms)")


async def main() -> None:
    await redis_failsafe()
    redaction()
    await bounded_llm()
    await cors_reload()
    await threads()
    await shared_http_client()
    broker_minimisation()
    atomic_counter()
    await loop_lag()


if __name__ == "__main__":
    rate_limiting_and_identity()
    asyncio.run(main())
