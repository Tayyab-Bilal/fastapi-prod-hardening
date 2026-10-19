import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_prod_hardening.identity import Hs256Verifier, Identity
from fastapi_prod_hardening.ratelimit import (
    CallerSettings,
    MemoryBackend,
    RateLimiter,
    RateLimitMiddleware,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def verifier() -> Hs256Verifier:
    return Hs256Verifier("test-secret")


@pytest.fixture
def token(verifier) -> str:
    return verifier.issue(Identity("t1", "u1"))


@pytest.fixture
def settings(verifier) -> CallerSettings:
    return CallerSettings({"svc-key": "worker"}, verifier)


def limited_app(settings, skip_paths=frozenset({"/health"}), limiter=None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limiter=limiter or RateLimiter(MemoryBackend()),
                       settings=settings, skip_paths=skip_paths)

    @app.get("/v1/tasks")
    def tasks():
        return {}

    @app.get("/health")
    def health():
        return {}

    return app


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(limited_app(settings))
