"""CORS allow-list from data, reloaded in the background, never queried per request."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from typing import Protocol
from urllib.parse import urlsplit

import anyio.to_thread
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)
_LOCAL = {"localhost", "127.0.0.1"}


def validate_origin(origin: str) -> str:
    """Normalise an origin for the admin add path, or raise ValueError."""
    if origin in ("*", "null", ""):
        raise ValueError("wildcard/null origin not allowed")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("origin must be http(s)://host[:port]")
    if "@" in parts.netloc:
        raise ValueError("userinfo not allowed")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("origin must not contain a path")
    if parts.scheme == "http" and parts.hostname not in _LOCAL:
        raise ValueError("http only allowed for localhost")
    return f"{parts.scheme}://{parts.netloc}"


class OriginSource(Protocol):
    def load(self) -> set[str]: ...


class SQLiteOriginSource:
    def __init__(self, path: str) -> None:
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS allowed_origins (origin TEXT PRIMARY KEY)")

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)  # one connection per call: safe across threads
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, origin: str) -> str:
        origin = validate_origin(origin)
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO allowed_origins VALUES (?)", (origin,))
        return origin

    def load(self) -> set[str]:
        with self._conn() as c:
            return {row[0] for row in c.execute("SELECT origin FROM allowed_origins")}


class DynamicCORS:
    def __init__(self, source: OriginSource, interval_s: float = 60) -> None:
        self.source, self.interval_s = source, interval_s
        self.origins: set[str] = set()
        self._task: asyncio.Task | None = None

    async def refresh(self) -> None:
        try:
            self.origins = await anyio.to_thread.run_sync(self.source.load)  # shared thread budget
        except Exception:  # source down: keep serving the last good list
            logger.warning("origin reload failed; keeping %d cached origins", len(self.origins))

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            await self.refresh()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """Echo the origin only if allowed. Add it outermost so 429s from inner layers get headers too."""

    def __init__(self, app, cors: DynamicCORS) -> None:
        super().__init__(app)
        self.cors = cors

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        allowed = origin is not None and origin in self.cors.origins
        if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
            if not allowed:
                return Response(status_code=403)
            response = Response(status_code=204, headers={
                "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": request.headers.get(
                    "access-control-request-headers", "*"),
                "Access-Control-Max-Age": "600"})
        else:
            response = await call_next(request)
        response.headers["Vary"] = "Origin"
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response
