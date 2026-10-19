# API reference

Public classes and functions, with the signature shape and what each is for. Runnable examples are
in [usage.md](usage.md).

## `fastapi_prod_hardening.app`

```text
create_app(*, secret="dev-secret", service_keys=None, mode="measure", origin_source=None,
           backend=None, llm=None, clock=time.monotonic, db_path=None, pool_size=30,
           debug_routes=False, http=None, settings=None) -> FastAPI
load_test_app() -> FastAPI
```

`create_app` builds the example API. `mode` is `"measure"` or `"enforce"` (identity check).
`pool_size` sizes both the DB pool and the shared thread limiter. `debug_routes=True` adds
`/debug/blocks-loop` and `/debug/threadpool` for the load test. `load_test_app` is the factory for
`uvicorn --factory`. Routes: `/health`, `/metrics`, `/v1/whoami`, `/v1/tasks` (POST),
`/v1/tasks/count`, `/v1/ask`.

## `fastapi_prod_hardening.ratelimit`

```text
CallerSettings(service_keys, verifier, user_limit=300, job_limit=3000, ip_limit=100,
               service_header="x-service-key", user_header="x-user-token")
caller_key(request, settings) -> (bucket, limit)
MemoryBackend(clock)            # per process
RedisBackend(redis_client)      # one Lua INCR+EXPIRE
RateLimiter(backend, window_s=60, redis_deadline_s=0.25, cooldown_s=30, clock)
    async hit(key, limit) -> Decision(allowed, retry_after)
RateLimitMiddleware(app, limiter, settings, skip_paths=frozenset({"/health"}))
```

Buckets: `user:` (valid service key and user token), `job:` (valid service key only), `ip:` (last
`X-Forwarded-For` hop). `skip_paths` is an exact-match set. A backend error or timeout allows the
request and starts the cooldown.

## `fastapi_prod_hardening.identity`

```text
Identity(tenant_id, user_id)
Hs256Verifier(secret, clock=time.time): issue(identity, ttl_s=3600) -> str; verify(token) -> Identity
InvalidToken
current_identity(request) -> Identity            # FastAPI dependency, 401 if missing or invalid
IdentityCheckMiddleware(app, verifier, mode="measure")
```

`measure` logs body-versus-token mismatches; `enforce` rejects them with 403. `Hs256Verifier` is a
stand-in for a real identity provider's verifier (anything with `verify(token) -> Identity`).

## `fastapi_prod_hardening.bounded_llm`

```text
BoundedLLM(providers, *, max_concurrency=50, call_deadline_s=240, per_attempt_timeout_s=60,
           stream_cap_s=270, background_stream_cap_s=900, breaker=None)
    async complete(prompt) -> str
    astream(prompt, *, caller_facing=False) -> AsyncIterator[str]
CircuitBreaker(failure_threshold=5, cooldown_s=30, clock): state, before_call(),
    record_success(), record_failure()
Provider (Protocol): complete(prompt), astream(prompt)
FakeProvider(name, latency=0.0, fail_times=0, tokens=...)
LLMError > CircuitOpen, DeadlineExceeded, AllProvidersFailed, StreamCapExceeded
```

`complete` raises `DeadlineExceeded`, `CircuitOpen` or `AllProvidersFailed`. `astream` raises
`StreamCapExceeded` at the cap and never falls back after the first token.

## `fastapi_prod_hardening.http`

```text
HttpClients(*, max_connections=100, max_keepalive=20, timeout=None, http2=None, transport=None)
    get() -> httpx.AsyncClient                     # one per running event loop
    async request(method, url, *, tenant_id=None, token=None, headers=None, **kw) -> Response
    async aclose()                                 # closes the current loop's client
```

`tenant_id` becomes `x-tenant-id` and `token` becomes `Authorization: Bearer ...`, on that request only.
`http2=None` means "on if `h2` is installed".

## `fastapi_prod_hardening.transcripts`

```text
TranscriptStore(path, clock=time.time, max_bytes=50 MB)
    put(job_id, text)            # PayloadTooLarge over the limit
    take(job_id) -> str | None   # returns and deletes
    sweep(older_than_s=86400) -> int
    count() -> int
log_task_start(logger, fn, *args, **kwargs) -> str   # logs argument names only
```

## `fastapi_prod_hardening.metrics`, `db`, `counters`, `threads`

```text
Metrics(lag_interval_s=0.05): loop_lag (LoopLagSampler), pool_wait (Histogram), render() -> str
LoopLagSampler(interval_s=0.05): start(), async stop(), last, max, histogram
Histogram(name, help_text, buckets): observe(value), render(), count, sum
Pool(path, size=30, wait_histogram=None): connection() -> context manager yielding sqlite3 connection
counters.init(conn); counters.increment(conn, name) -> int; counters.get(conn, name) -> int
shared_limiter(total=30) -> CapacityLimiter       # call inside the running loop
```

## `fastapi_prod_hardening.cors`

```text
validate_origin(origin) -> str                    # ValueError for *, null, paths, userinfo, non-local http
SQLiteOriginSource(path): add(origin), load() -> set[str]
DynamicCORS(source, interval_s=60): async refresh(), async start(), async stop(), origins
DynamicCORSMiddleware(app, cors)
```

## `fastapi_prod_hardening.redaction`

```text
SecretScrubFormatter(fmt=None, secrets=()): scrub(text) -> str
install_redaction(root=None, secrets=())          # re-run after a framework replaces handlers
```

## `fastapi_prod_hardening.strict_skips` (pytest plugin)

Adds `--strict-skips`: the run fails if any test is skipped (expected failures are not counted).

## Scripts

```text
python scripts/check_junit.py REPORT [--max-skips N]   exit 1 on missing, empty, all-skipped, failed
python scripts/check_migrations.py FOLDER              exit 1 on duplicate id, missing parent, cycle, multiple heads
python scripts/loadtest.py BASE_URL PATH [--clients N] [--seconds S]
```
