"""Identity comes from the signed token, never from the request body.

A caller-supplied ``tenant_id`` used for authorisation or billing lets any user act as any tenant.
The identity middleware has two modes so the fix can ship safely: ``measure`` logs every mismatch
(find out which clients still send wrong ids), ``enforce`` rejects them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("identity_check")


@dataclass(frozen=True)
class Identity:
    tenant_id: str
    user_id: str


class InvalidToken(Exception):
    pass


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Identity: ...


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Hs256Verifier:
    """Minimal HS256 token check with the stdlib; swap for a real JWT/JWKS verifier in production."""

    def __init__(self, secret: str, clock: Callable[[], float] = time.time) -> None:
        self._key = secret.encode()
        self._clock = clock

    def _sign(self, signing_input: str) -> bytes:
        return hmac.new(self._key, signing_input.encode(), hashlib.sha256).digest()

    def issue(self, identity: Identity, ttl_s: int = 3600) -> str:
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        data = {"sub": identity.user_id, "tid": identity.tenant_id,
                  "exp": int(self._clock()) + ttl_s}
        payload = _b64(json.dumps(data).encode())
        return f"{header}.{payload}.{_b64(self._sign(f'{header}.{payload}'))}"

    def verify(self, token: str) -> Identity:
        try:
            header, payload, sig = token.split(".")
            if json.loads(_unb64(header)).get("alg") != "HS256":
                raise InvalidToken("unsupported alg")
            if not hmac.compare_digest(_unb64(sig), self._sign(f"{header}.{payload}")):
                raise InvalidToken("bad signature")
            data = json.loads(_unb64(payload))
            if data["exp"] < self._clock():
                raise InvalidToken("expired")
            return Identity(tenant_id=str(data["tid"]), user_id=str(data["sub"]))
        except InvalidToken:
            raise
        except Exception as exc:  # malformed input of any kind is just "invalid"
            raise InvalidToken("malformed token") from exc


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else None


def current_identity(request: Request) -> Identity:
    """FastAPI dependency: the only place route code should learn who the caller is."""
    token = bearer_token(request)
    if not token:
        raise HTTPException(401, "missing bearer token")
    try:
        return request.app.state.verifier.verify(token)
    except InvalidToken as exc:
        raise HTTPException(401, "invalid token") from exc


class IdentityCheckMiddleware(BaseHTTPMiddleware):
    """Compare body/query ``tenant_id``/``user_id`` with the token's identity."""

    def __init__(self, app, verifier: TokenVerifier, mode: Literal["measure", "enforce"] = "measure"):
        super().__init__(app)
        self.verifier, self.mode = verifier, mode

    async def dispatch(self, request: Request, call_next):
        token = bearer_token(request)
        if token:
            try:
                ident = self.verifier.verify(token)
            except InvalidToken:
                ident = None  # the route dependency answers 401; nothing to compare against
            if ident:
                mismatches = await self._mismatches(request, ident)
                if mismatches:
                    logger.warning(json.dumps({"event": "identity_mismatch", "mode": self.mode,
                                               "path": request.url.path, "fields": mismatches,
                                               "claimed_tenant": ident.tenant_id}))
                    if self.mode == "enforce":
                        return JSONResponse({"detail": "identity mismatch"}, status_code=403)
        return await call_next(request)

    @staticmethod
    async def _mismatches(request: Request, ident: Identity) -> list[str]:
        claimed: dict = dict(request.query_params)
        if "json" in request.headers.get("content-type", ""):
            try:
                body = json.loads(await request.body())
                if isinstance(body, dict):
                    claimed.update(body)
            except ValueError:
                pass
        expected = {"tenant_id": ident.tenant_id, "user_id": ident.user_id}
        return [k for k, v in expected.items() if k in claimed and str(claimed[k]) != v]
