import asyncio
import hmac

import fakeredis.aioredis
from conftest import Clock, limited_app
from fastapi.testclient import TestClient
from starlette.requests import Request

from fastapi_prod_hardening.cors import DynamicCORS, DynamicCORSMiddleware
from fastapi_prod_hardening.ratelimit import RateLimiter, RedisBackend, caller_key


def req(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": ("9.9.9.9", 1), "method": "GET",
                    "path": "/"})


def test_user_bucket_needs_valid_service_key(settings, token):
    assert caller_key(req({"x-service-key": "svc-key", "x-user-token": token}), settings) == (
        "user:u1", 300)
    # a user token alone, or with a wrong service key, must not select the user bucket
    for headers in ({"x-user-token": token}, {"x-service-key": "nope", "x-user-token": token}):
        bucket, limit = caller_key(req(headers), settings)
        assert bucket.startswith("ip:") and limit == 100


def test_service_key_compared_constant_time(settings, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    caller_key(req({"x-service-key": "svc-key"}), settings)
    assert calls and all(isinstance(a, bytes) for a, _ in calls)


def test_job_bucket_higher_limit(settings):
    c = TestClient(limited_app(settings))
    ip_codes = [c.get("/v1/tasks").status_code for _ in range(101)]
    job_codes = [c.get("/v1/tasks", headers={"x-service-key": "svc-key"}).status_code
                 for _ in range(101)]
    assert ip_codes[-1] == 429 and set(job_codes) == {200}


def test_ip_from_last_forwarded_hop(settings):
    assert caller_key(req({"x-forwarded-for": "6.6.6.6, 10.0.0.1, 1.2.3.4"}), settings) == (
        "ip:1.2.3.4", 100)
    # rotating the forged leftmost entry must not reset the bucket
    c = TestClient(limited_app(settings))
    codes = [c.get("/v1/tasks", headers={"x-forwarded-for": f"{i}.0.0.1, 1.2.3.4"}).status_code
             for i in range(101)]
    assert codes[-1] == 429


def test_preflight_not_counted(settings):
    backend_hits = []

    class Spy:
        async def incr(self, key, window_s):
            backend_hits.append(key)
            return 1, 60

    c = TestClient(limited_app(settings, limiter=RateLimiter(Spy())))
    c.options("/v1/tasks", headers={"origin": "https://a.example",
                                    "access-control-request-method": "GET"})
    assert backend_hits == []
    c.get("/v1/tasks")
    assert len(backend_hits) == 1


def test_429_has_cors_and_retry_after(settings):
    app = limited_app(settings)
    cors = DynamicCORS(source=None)
    cors.origins = {"https://app.example"}
    app.add_middleware(DynamicCORSMiddleware, cors=cors)  # outermost, like create_app
    c = TestClient(app)
    for _ in range(100):
        c.get("/v1/tasks")
    r = c.get("/v1/tasks", headers={"origin": "https://app.example"})
    assert r.status_code == 429
    assert r.headers["access-control-allow-origin"] == "https://app.example"
    assert int(r.headers["retry-after"]) >= 1


def test_root_prefix_skip_list_would_bypass_everything(settings):
    def buggy_prefix_skip(path: str, prefixes=("/", "/health")) -> bool:
        return any(path.startswith(p) for p in prefixes)

    assert buggy_prefix_skip("/v1/tasks")  # the known pitfall: "/" matches every path
    # our exact-path list holds "/" too, yet real routes are still limited
    c = TestClient(limited_app(settings, skip_paths=frozenset({"/", "/health"})))
    assert [c.get("/v1/tasks").status_code for _ in range(101)][-1] == 429
    assert all(c.get("/health").status_code == 200 for _ in range(150))


async def test_redis_lua_counts_atomically():
    redis = fakeredis.aioredis.FakeRedis()
    backend = RedisBackend(redis)
    results = await asyncio.gather(*(backend.incr("k", 60) for _ in range(250)))
    assert sorted(c for c, _ in results) == list(range(1, 251))  # no lost or duplicate counts
    assert 0 < await redis.ttl("rl:k") <= 60
    limiter = RateLimiter(RedisBackend(redis))
    decisions = [await limiter.hit("other", 100) for _ in range(101)]
    assert [d.allowed for d in decisions].count(True) == 100


async def test_redis_failure_fails_open_with_cooldown():
    clock = Clock()

    class Broken:
        calls = 0

        async def incr(self, key, window_s):
            self.calls += 1
            raise ConnectionError("redis down")

    backend = Broken()
    limiter = RateLimiter(backend, clock=clock)
    assert (await limiter.hit("k", 1)).allowed
    assert (await limiter.hit("k", 1)).allowed
    assert backend.calls == 1  # second request never touched Redis
    clock.now += 31
    await limiter.hit("k", 1)
    assert backend.calls == 2  # cooldown over, Redis is tried again


async def test_redis_hang_hits_deadline_then_fails_open():
    class Hung:
        async def incr(self, key, window_s):
            await asyncio.sleep(5)

    limiter = RateLimiter(Hung(), redis_deadline_s=0.02)
    assert (await asyncio.wait_for(limiter.hit("k", 1), 1)).allowed
