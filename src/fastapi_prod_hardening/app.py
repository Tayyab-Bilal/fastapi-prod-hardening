"""Example "Acme Workspace" API wiring every piece together."""

from __future__ import annotations

import logging
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Callable

import anyio.to_thread
from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import counters
from .bounded_llm import BoundedLLM, FakeProvider
from .cors import DynamicCORS, DynamicCORSMiddleware, OriginSource
from .db import Pool
from .http import HttpClients
from .identity import Hs256Verifier, Identity, IdentityCheckMiddleware, current_identity
from .metrics import Metrics
from .ratelimit import Backend, CallerSettings, MemoryBackend, RateLimiter, RateLimitMiddleware
from .redaction import install_redaction
from .threads import shared_limiter


class TaskIn(BaseModel):
    title: str
    tenant_id: str | None = None  # accepted but never trusted; the identity middleware watches it


class _NoOrigins:
    def load(self) -> set[str]:
        return set()


def create_app(*, secret: str = "dev-secret", service_keys: dict[str, str] | None = None,
               mode: str = "measure", origin_source: OriginSource | None = None,
               backend: Backend | None = None, llm: BoundedLLM | None = None,
               clock: Callable[[], float] = time.monotonic, db_path: str | None = None,
               pool_size: int = 30, debug_routes: bool = False,
               http: HttpClients | None = None,
               settings: CallerSettings | None = None) -> FastAPI:
    verifier = Hs256Verifier(secret)
    cors = DynamicCORS(origin_source or _NoOrigins())
    settings = settings or CallerSettings(service_keys or {"demo-service-key": "worker"}, verifier)
    metrics = Metrics()
    pool = Pool(db_path or f"{tempfile.mkdtemp()}/acme.db", pool_size, metrics.pool_wait)
    with pool.connection() as conn:
        counters.init(conn)
    http = http or HttpClients()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        install_redaction(logging.getLogger(), secrets=[secret])
        shared_limiter(pool_size)  # same number as the DB pool: threads never outnumber connections
        metrics.loop_lag.start()
        await cors.start()
        yield
        await cors.stop()
        await metrics.loop_lag.stop()
        await http.aclose()

    app = FastAPI(title="Acme Workspace API", lifespan=lifespan)
    app.state.verifier, app.state.cors = verifier, cors
    app.state.metrics, app.state.pool, app.state.http = metrics, pool, http
    llm = llm or BoundedLLM([FakeProvider("primary", fail_times=1), FakeProvider("backup")])

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/v1/whoami")
    async def whoami(ident: Identity = Depends(current_identity)) -> dict:
        return {"tenant_id": ident.tenant_id, "user_id": ident.user_id}

    def _next_task_number() -> int:
        with pool.connection() as conn:
            return counters.increment(conn, "tasks")

    @app.post("/v1/tasks")
    async def create_task(task: TaskIn, ident: Identity = Depends(current_identity)) -> dict:
        # Mixed route: async because it also does non-DB async work; its DB block is offloaded
        # through anyio so it draws from the same thread budget as the sync route below.
        number = await anyio.to_thread.run_sync(_next_task_number)
        return {"title": task.title, "tenant_id": ident.tenant_id,  # tenant from token, not body
                "task_number": number}

    @app.get("/v1/tasks/count")
    def task_count(ident: Identity = Depends(current_identity)) -> dict:
        # DB-only route: a plain `def`, so FastAPI runs it on the thread pool and the loop stays free.
        with pool.connection() as conn:
            return {"count": counters.get(conn, "tasks")}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics_route() -> str:
        return metrics.render()

    if debug_routes:  # for scripts/loadtest.py only: one bad pattern, one good one

        @app.get("/debug/blocks-loop")
        async def blocks_loop() -> dict:
            time.sleep(0.05)  # BAD on purpose: blocking call inside async def freezes every request
            return {"ok": True}

        @app.get("/debug/threadpool")
        def threadpool() -> dict:
            time.sleep(0.05)  # same work, but on the thread pool: the loop stays responsive
            return {"ok": True}

    @app.post("/v1/ask")
    async def ask(ident: Identity = Depends(current_identity)) -> dict:
        return {"answer": await llm.complete("summarise my tasks")}

    # Last added is outermost: CORS wraps the limiter so 429s still carry CORS headers.
    app.add_middleware(IdentityCheckMiddleware, verifier=verifier, mode=mode)
    app.add_middleware(RateLimitMiddleware, limiter=RateLimiter(backend or MemoryBackend(clock),
                                                                 clock=clock), settings=settings,
                       skip_paths=frozenset({"/health", "/metrics"}))
    app.add_middleware(DynamicCORSMiddleware, cors=cors)
    return app


def load_test_app() -> FastAPI:
    """Factory for ``uvicorn --factory``: debug routes on and a limit high enough for a load test."""
    verifier = Hs256Verifier("dev-secret")
    return create_app(debug_routes=True, settings=CallerSettings(
        {"demo-service-key": "worker"}, verifier, ip_limit=10**9))
