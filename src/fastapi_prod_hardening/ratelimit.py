"""Per-caller rate limiting that fails safe.

Behind one gateway every request has the same source IP, so an IP bucket is one bucket for the whole
product. We key on the caller instead, and only trust the user token when it arrives together with
a valid service key (otherwise anyone could pick any bucket by sending a header).
"""

from __future__ import annotations

import asyncio
import hmac
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .identity import InvalidToken, TokenVerifier


@dataclass
class CallerSettings:
    service_keys: dict[str, str]  # service key -> service name
    verifier: TokenVerifier
    user_limit: int = 300
    job_limit: int = 3000
    ip_limit: int = 100
    service_header: str = "x-service-key"
    user_header: str = "x-user-token"


def _valid_service(request: Request, settings: CallerSettings) -> str | None:
    supplied = request.headers.get(settings.service_header, "").encode()
    found = None
    for key, name in settings.service_keys.items():  # no early exit: timing stays flat
        if hmac.compare_digest(supplied, key.encode()):
            found = name
    return found


def caller_key(request: Request, settings: CallerSettings) -> tuple[str, int]:
    service = _valid_service(request, settings)
    if service:
        token = request.headers.get(settings.user_header)
        if token:
            try:
                return f"user:{settings.verifier.verify(token).user_id}", settings.user_limit
            except InvalidToken:
                pass  # a bad user token from a trusted service is still that service's traffic
        return f"job:{service}", settings.job_limit
    # Last hop, not first: the gateway appends the real peer, the leftmost entry is client-forged.
    hops = request.headers.get("x-forwarded-for", "")
    ip = hops.split(",")[-1].strip() if hops else (request.client.host if request.client else "?")
    return f"ip:{ip}", settings.ip_limit


class Backend(Protocol):
    async def incr(self, key: str, window_s: int) -> tuple[int, int]:
        """Count this hit; return (count in window, seconds until the window resets)."""


class MemoryBackend:
    # Simplification: per-process counters, so N workers allow N x limit; use RedisBackend when shared.
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, tuple[int, float]] = {}

    async def incr(self, key: str, window_s: int) -> tuple[int, int]:
        now = self._clock()
        count, expires = self._hits.get(key, (0, now + window_s))
        if now >= expires:
            count, expires = 0, now + window_s
        self._hits[key] = (count + 1, expires)
        return count + 1, max(1, int(expires - now))


# INCR + EXPIRE-on-first-hit in one script: two separate calls could die between them and leave an
# immortal counter that blocks the caller forever.
_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {c, redis.call('TTL', KEYS[1])}
"""


class RedisBackend:
    def __init__(self, client) -> None:  # redis.asyncio.Redis (or fakeredis equivalent)
        self._script = client.register_script(_LUA)
        # A bounded pool raises MaxConnectionsError when a burst has more hits in flight than
        # connections; waiting for a free slot here keeps a burst a queue, not an error that trips
        # the fail-open cooldown. Each hit holds one connection at a time.
        pool = getattr(client, "connection_pool", None)
        self._slots = asyncio.Semaphore(getattr(pool, "max_connections", None) or 2**31)

    async def incr(self, key: str, window_s: int) -> tuple[int, int]:
        async with self._slots:
            count, ttl = await self._script(keys=[f"rl:{key}"], args=[window_s])
        return int(count), max(1, int(ttl))


@dataclass
class Decision:
    allowed: bool
    retry_after: int = 0


@dataclass
class RateLimiter:
    backend: Backend
    window_s: int = 60
    redis_deadline_s: float = 0.25
    cooldown_s: float = 30
    clock: Callable[[], float] = time.monotonic
    _cooldown_until: float = field(default=0.0, init=False)

    async def hit(self, key: str, limit: int) -> Decision:
        if self.clock() < self._cooldown_until:
            return Decision(True)  # backend recently broken: don't make every request wait on it
        try:
            count, ttl = await asyncio.wait_for(self.backend.incr(key, self.window_s),
                                                self.redis_deadline_s)
        except Exception:  # timeout or any backend error: availability beats strict limiting
            self._cooldown_until = self.clock() + self.cooldown_s
            return Decision(True)
        return Decision(count <= limit, ttl)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """``skip_paths`` is an exact-match set on purpose: a prefix list containing "/" skips every path."""

    def __init__(self, app, limiter: RateLimiter, settings: CallerSettings,
                 skip_paths: frozenset[str] = frozenset({"/health"})):
        super().__init__(app)
        self.limiter, self.settings, self.skip_paths = limiter, settings, skip_paths

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in self.skip_paths:
            return await call_next(request)
        bucket, limit = caller_key(request, self.settings)
        decision = await self.limiter.hit(bucket, limit)
        if not decision.allowed:
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                                headers={"Retry-After": str(decision.retry_after)})
        return await call_next(request)
