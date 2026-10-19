import threading
import time

import anyio
import anyio.to_thread
import httpx
import pytest
from fastapi import FastAPI
from tests_helpers import load_script

from fastapi_prod_hardening import counters
from fastapi_prod_hardening.app import create_app, load_test_app
from fastapi_prod_hardening.threads import shared_limiter


def test_atomic_counter_never_loses_updates_under_concurrency(tmp_path):
    from fastapi_prod_hardening.db import Pool
    pool = Pool(str(tmp_path / "c.db"), size=8)
    with pool.connection() as c:
        counters.init(c)

    def work():
        for _ in range(50):
            with pool.connection() as c:
                counters.increment(c, "n")

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with pool.connection() as c:
        assert counters.get(c, "n") == 400


def test_increment_returns_the_new_value(tmp_path):
    from fastapi_prod_hardening.db import Pool
    pool = Pool(str(tmp_path / "c.db"))
    with pool.connection() as c:
        counters.init(c)
        assert [counters.increment(c, "a"), counters.increment(c, "a"), counters.increment(c, "b")] == [1, 2, 1]


async def test_sync_routes_and_offloads_share_one_thread_budget():
    limiter = shared_limiter(3)
    running = peak = 0
    lock = threading.Lock()

    def work():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1

    app = FastAPI()

    @app.get("/sync")
    def sync_route():  # DB-only style route: FastAPI runs it on anyio's thread pool
        work()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with anyio.create_task_group() as tg:
            for _ in range(6):
                tg.start_soon(client.get, "/sync")  # sync routes
            for _ in range(6):
                tg.start_soon(anyio.to_thread.run_sync, work)  # mixed-route offloads
    assert limiter.total_tokens == 3 and 1 < peak <= 3  # 12 jobs, one budget of 3


def test_lifespan_sizes_thread_limiter_to_pool():
    from fastapi.testclient import TestClient

    async def total():
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    app = create_app(pool_size=12)
    with TestClient(app) as c:
        assert c.portal.call(total) == 12


def test_app_routes_use_the_right_kind_of_function():
    import inspect
    app = create_app()
    routes = {r.path: r.endpoint for r in app.routes if hasattr(r, "endpoint")}
    assert not inspect.iscoroutinefunction(routes["/v1/tasks/count"])  # DB-only: sync def
    assert inspect.iscoroutinefunction(routes["/v1/tasks"])  # mixed: async + offload


async def test_loadtest_canary_slows_down_only_when_loop_is_blocked():
    loadtest = load_script("loadtest")
    app = load_test_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            blocked = await loadtest.run_load(client, "/debug/blocks-loop", clients=5, seconds=0.6)
            pooled = await loadtest.run_load(client, "/debug/threadpool", clients=5, seconds=0.6)
    assert blocked["canary_max_ms"] >= 40
    assert pooled["canary_max_ms"] < blocked["canary_max_ms"]
    assert pooled["requests"] > blocked["requests"]


@pytest.mark.parametrize("n", [2])
def test_post_task_returns_increasing_numbers(token, n):
    from fastapi.testclient import TestClient
    with TestClient(create_app(secret="test-secret")) as c:
        h = {"authorization": f"Bearer {token}"}
        nums = [c.post("/v1/tasks", json={"title": "t"}, headers=h).json()["task_number"] for _ in range(n)]
        assert nums == [1, 2] and c.get("/v1/tasks/count", headers=h).json() == {"count": 2}
