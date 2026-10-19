"""One shared HTTP client per event loop, with per-request tenant headers."""

from __future__ import annotations

import asyncio
import importlib.util
import weakref
from typing import Any

import httpx


class HttpClients:
    """Lifecycle-managed ``httpx.AsyncClient`` singleton.

    Why a singleton: a new client per call throws away keep-alive connections and repeats the TLS
    handshake; reusing one made later calls about 2.5x faster in the original service. Why *per
    loop*: an async client binds its connections to the loop that opened them, and a worker that
    calls ``asyncio.run()`` repeatedly would crash on the second loop.

    Why headers are per call: the client is shared by every tenant. Anything stored on it (a token,
    a tenant id) would leak into another tenant's request, so ``request`` takes them as arguments.
    """

    def __init__(self, *, max_connections: int = 100, max_keepalive: int = 20,
                 timeout: httpx.Timeout | None = None, http2: bool | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.limits = httpx.Limits(max_connections=max_connections,
                                   max_keepalive_connections=max_keepalive)
        self.timeout = timeout or httpx.Timeout(30.0, connect=5.0)
        # HTTP/2 needs the optional `h2` package; fall back to HTTP/1.1 instead of failing at boot.
        self.http2 = (importlib.util.find_spec("h2") is not None) if http2 is None else http2
        self.transport = transport
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
            weakref.WeakKeyDictionary())

    def get(self) -> httpx.AsyncClient:
        """The client for the running loop, created on first use."""
        loop = asyncio.get_running_loop()
        if loop not in self._clients:
            self._clients[loop] = httpx.AsyncClient(
                limits=self.limits, timeout=self.timeout, http2=self.http2,
                transport=self.transport)
        return self._clients[loop]

    async def request(self, method: str, url: str, *, tenant_id: str | None = None,
                      token: str | None = None, headers: dict[str, str] | None = None,
                      **kwargs: Any) -> httpx.Response:
        merged = dict(headers or {})
        if tenant_id:
            merged["x-tenant-id"] = tenant_id
        if token:
            merged["authorization"] = f"Bearer {token}"
        return await self.get().request(method, url, headers=merged, **kwargs)

    async def aclose(self) -> None:
        """Close this loop's client (call from the lifespan shutdown, on the same loop)."""
        client = self._clients.pop(asyncio.get_running_loop(), None)
        if client is not None:
            await client.aclose()
