# fastapi-prod-hardening

Hardening patterns for a multi-tenant FastAPI service that calls LLMs, each one a small,
tested module you can lift on its own.

> An open-source reference implementation of hardening patterns for a multi-tenant FastAPI service that calls LLMs. All names and data are fictional.

[![CI](https://github.com/Tayyab-Bilal/fastapi-prod-hardening/actions/workflows/ci.yml/badge.svg)](https://github.com/Tayyab-Bilal/fastapi-prod-hardening/actions/workflows/ci.yml)

## At a glance

**What it is.** A small, runnable reference for the safeguards a multi-tenant FastAPI service needs when it calls LLMs. Each safeguard is its own tested module, and the LLM, Redis and database are all fakes, so everything runs offline.

**What is built**
- Per-caller rate limiting with a Redis Lua counter that fails open (`ratelimit.py: caller_key`, `RateLimiter.hit`).
- Identity taken from the signed token, with a measure mode and an enforce mode (`identity.py: IdentityCheckMiddleware`).
- Log redaction at the handler (`redaction.py: SecretScrubFormatter`).
- Bounded LLM calls: concurrency cap, whole-call deadline, circuit breaker, stream caps (`bounded_llm.py: BoundedLLM`).
- A shared HTTP client per event loop (`http.py: HttpClients`).
- Big payloads passed by job id through a database table (`transcripts.py: TranscriptStore`).
- One thread budget for sync routes and offloaded work, sized to the DB pool (`threads.py: shared_limiter`).
- Atomic counter, database-backed CORS list, event-loop and pool-wait metrics, CI gates.

**What is not built or simplified**
- The LLM providers, Redis and Postgres are fakes (`FakeProvider`, `fakeredis`, SQLite).
- `MemoryBackend` counts per process, so several workers each allow the full limit.
- `Hs256Verifier` is a small stand-in, not a full JWT library.
- Streams never switch provider mid-stream.
- Metrics are in process and `/metrics` has no auth.

**How it flows**
1. The CORS layer runs first, so even a rejected request carries CORS headers.
2. The rate limiter picks a bucket for the caller and counts the hit.
3. The identity check compares the token's tenant with any tenant named in the request.
4. The route runs on the thread pool (DB-only) or offloads its DB block to it (mixed).
5. LLM calls take a slot, then try each provider once, in order, inside a deadline.
6. Outgoing HTTP goes through one shared client that gets tenant headers per call.
7. Logs are scrubbed of secrets as they are written.

**Run it**
```bash
.venv/bin/pytest -q    # 100 passed, about 10 s, no network
```

## How each part works, one by one

### 1. Rate limit per caller, not per IP
- **Risk / problem:** Behind one gateway every request has the same source address, so an IP limit puts the whole product in one bucket. A header that picks the bucket can also be forged.
- **How it works:**
  - The limiter picks a bucket from who is calling: a user, a background job, or an anonymous address. Users get 300 hits a minute, jobs 3,000 and anonymous addresses 100 (`ratelimit.py: caller_key`).
  - The user token only counts if the request also carries a valid service key. Without it, anyone could claim any user's bucket by sending a header (`ratelimit.py: caller_key`).
  - The service key is compared in constant time and the loop never stops early, so response timing does not leak how much of a guess was right (`ratelimit.py: _valid_service`).
  - For anonymous callers it uses the last address in the forwarded list, because the gateway adds the real peer at the end and the first entry can be forged by the client (`ratelimit.py: caller_key`).
  - The skip list for health checks is an exact match on the path. A prefix list containing just the root path would match every URL and switch the limiter off (`ratelimit.py: RateLimitMiddleware`).
- **Tests that prove it:** `tests/test_ratelimit.py`: `test_user_bucket_needs_valid_service_key`, `test_ip_from_last_forwarded_hop`, `test_root_prefix_skip_list_would_bypass_everything`, and 7 more in that file.

### 2. Atomic counting that fails open
- **Risk / problem:** A counter built from two Redis calls can die in between and leave a key that never expires, blocking a caller for good. A hung Redis would also make every request wait.
- **How it works:**
  - The count and the expiry are set in one Lua script that Redis runs as a single step, so a counter always gets an expiry on its first hit (`ratelimit.py: RedisBackend`).
  - The window is one minute, and the answer includes how long until it resets, which becomes the retry hint on a 429 (`ratelimit.py: RateLimiter.hit`).
  - Each Redis call gets a quarter of a second. If it times out or errors, the request is let through, because availability matters more than strict limiting (`ratelimit.py: RateLimiter.hit`).
  - After a failure the limiter stops calling Redis for 30 seconds, so a broken Redis costs nothing per request while it is down (`ratelimit.py: RateLimiter.hit`).
  - Browser preflight requests are not counted, so one page load does not burn the budget.
- **Tests that prove it:** `tests/test_ratelimit.py`: `test_redis_lua_counts_atomically`, `test_redis_failure_fails_open_with_cooldown`, `test_redis_hang_hits_deadline_then_fails_open`, and 7 more in that file.

### 3. Identity comes from the token, not the body
- **Risk / problem:** If the service trusts a tenant id sent in the request body, any user can act as any other tenant.
- **How it works:**
  - Routes learn who the caller is from one dependency that reads the signed token. The body is never used for that (`identity.py: current_identity`).
  - The token is checked for a known algorithm, a matching signature (constant-time compare) and an expiry. Anything malformed counts as invalid (`identity.py: Hs256Verifier.verify`).
  - A middleware compares any tenant or user id in the body or query with the token. A difference is a mismatch (`identity.py: IdentityCheckMiddleware`).
  - In measure mode it only logs each mismatch and lets the request through, so you can find clients that still send wrong ids. In enforce mode it answers 403. This lets the fix ship without breaking clients nobody had counted.
  - The signer here is a small stand-in. A real deployment would use a proper JWT library.
- **Tests that prove it:** `tests/test_identity.py`: `test_identity_from_token_not_body`, `test_measure_mode_logs_mismatch_and_allows`, `test_enforce_mode_rejects_mismatch`, `test_tampered_token_rejected`.

### 4. Redact secrets at the handler
- **Risk / problem:** Passwords in connection strings, bearer tokens and API keys end up in logs. A filter on the parent logger misses records from child loggers.
- **How it works:**
  - Records from child loggers go straight to the parent's handlers and skip the parent's filters. So the scrubbing sits on the handler's formatter, which sees every line that is actually written (`redaction.py: SecretScrubFormatter`).
  - It scrubs the final text, so exception messages and formatted arguments are covered too (`redaction.py: SecretScrubFormatter.format`).
  - It blanks passwords inside connection strings, bearer tokens, key-shaped strings and any exact secret values you hand it.
  - Scrubbing twice gives the same result, so stacking it by accident is harmless.
  - Frameworks sometimes replace log handlers at startup, so the install function is meant to be run again afterwards (`redaction.py: install_redaction`).
- **Tests that prove it:** `tests/test_redaction.py`: `test_child_logger_record_redacted_at_handler`, `test_logger_level_filter_misses_propagated_record`, `test_reinstall_after_framework_replaces_handlers`, and 3 more in that file.

### 5. Every LLM call is bounded
- **Risk / problem:** Without limits, one slow model can hold a request for minutes, a fallback chain can multiply the wait, and a dead provider keeps getting hit.
- **How it works:**
  - A semaphore caps concurrent calls at 50. The semaphore is kept per event loop, because one made on a different loop would crash (`bounded_llm.py: BoundedLLM._semaphore`).
  - The whole call has a 240 second deadline and each attempt gets at most 60 seconds, trimmed to whatever time is left (`bounded_llm.py: BoundedLLM.complete`).
  - Each provider is tried once, in order. Fallbacks never retry, so the waits do not multiply (`bounded_llm.py: BoundedLLM.complete`).
  - The circuit breaker is asked before every attempt. After five failures it opens and calls fail at once without sending anything, then it lets one trial through after 30 seconds (`bounded_llm.py: CircuitBreaker`).
  - Streams use the first healthy provider. One that fails before its first token is skipped, but after the first token there is no switch, because it would splice two answers together. This is a deliberate simplification.
  - A stream a person is waiting on is cut off at 270 seconds. Background streams get a 900 second backstop (`bounded_llm.py: BoundedLLM.astream`).
- **Tests that prove it:** `tests/test_bounded_llm.py`: `test_deadline_stops_new_fallbacks`, `test_open_circuit_fails_fast_with_zero_calls`, `test_fallbacks_do_not_retry`, and 4 more in that file. Also `tests/test_streams.py`: `test_stream_does_not_fall_back_after_first_token`, `test_stream_uses_first_healthy_provider`, `test_stream_all_providers_failing_raises`.

### 6. Large payloads travel by job id
- **Risk / problem:** A long transcript put on a message broker is copied into queues, retry state, task screens and log indexes, and it can contain personal data.
- **How it works:**
  - The text is written to a database table and the task message carries only the job id (`transcripts.py: TranscriptStore.put`).
  - The worker reads it with one statement that returns the text and deletes the row together, so delivery and cleanup cannot drift apart (`transcripts.py: TranscriptStore.take`).
  - A sweep deletes rows older than 24 hours, which cleans up after a worker that was killed before it took its job (`transcripts.py: TranscriptStore.sweep`).
  - Anything over 50 MB is refused up front (`transcripts.py: TranscriptStore.put`).
  - The task start log prints the names of the arguments and never their values (`transcripts.py: log_task_start`).
- **Tests that prove it:** `tests/test_transcripts.py`: `test_take_returns_body_and_deletes_it`, `test_task_message_carries_only_the_job_id`, `test_task_start_log_prints_argument_names_only`, and 3 more in that file.

### 7. One thread budget for the event loop
- **Risk / problem:** Blocking database calls on the event loop freeze every request. Extra threads beyond the database pool just queue on connections, which looks like a slow database.
- **How it works:**
  - Routes that only touch the database are plain sync functions, so FastAPI runs them on a thread pool. Mixed routes stay async and hand each database block to a thread (`app.py: create_app`).
  - Both paths draw from one thread limiter, set to the same size as the database pool (30 by default). Waiting happens in a queue instead of on the pool (`threads.py: shared_limiter`).
  - Offloads must use the anyio call, not the asyncio one, because the asyncio one has its own separate budget.
  - The pool records how long each caller waited for a connection, and a sampler records event-loop lag. Both show on the metrics page, so a fix can be measured (`db.py: Pool.connection`, `metrics.py`).
- **Tests that prove it:** `tests/test_hygiene.py`: `test_sync_routes_and_offloads_share_one_thread_budget`, `test_lifespan_sizes_thread_limiter_to_pool`, `test_loadtest_canary_slows_down_only_when_loop_is_blocked`, and 4 more in that file. Also `tests/test_metrics.py`: `test_pool_wait_is_recorded_when_pool_is_exhausted`.

## The problem

A multi-tenant FastAPI service that calls LLMs needs the same handful of safeguards: working rate
limits, identity taken from the verified token, secrets kept out of logs, a bound on every LLM wait,
a strict CORS list, and no blocking database calls on the event loop.

This repo implements those safeguards in a fictional "Acme Workspace" API (projects, tasks, documents,
meetings), with fakes in place of the LLM providers, Redis and Postgres, so every claim here can be
checked by running the tests.

## What it does

| Capability | Code |
|---|---|
| Per-caller rate limits (user 300/min, job 3,000/min, other by last forwarded hop 100/min); atomic Redis Lua counter; fail open with a cooldown | `ratelimit.py` |
| Identity from the signed token, never the request body; measure mismatches first, then enforce | `identity.py` |
| Log redaction at the handler, re-installable; connection-string passwords scrubbed | `redaction.py` |
| Bounded LLM calls: concurrency cap, whole-call deadline, call-time breaker check, caller-facing stream cap | `bounded_llm.py` |
| Lifecycle-managed shared HTTP client, one per event loop, HTTP/2 when available, tenant headers per request | `http.py` |
| Large payloads travel by job id through a database table, deleted after delivery, 24 h sweep, 50 MB limit, argument names only in logs | `transcripts.py` |
| Event-loop hygiene: sync `def` for DB-only routes, `anyio.to_thread` for mixed routes, one shared thread budget sized to the DB pool | `app.py`, `threads.py`, `db.py` |
| Event-loop lag and DB pool-wait metrics at `/metrics`, with a `/health` canary load test | `metrics.py`, `scripts/loadtest.py` |
| Atomic counter with `UPDATE ... RETURNING` (no lost updates) | `counters.py` |
| CORS allow-list from the database, reloaded in the background, validated on add | `cors.py` |
| CI gates: JUnit report check, `--strict-skips`, migration-graph check, change-aware workflow templates | `scripts/`, `strict_skips.py`, `.github/workflows/` |

## Quickstart

```bash
uv venv .venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/python examples/demo.py
```

Everything runs offline: a scripted `FakeProvider` for LLMs, `fakeredis[lua]` for Redis, SQLite for
the database, `httpx.MockTransport` for outgoing HTTP, injectable clocks. `httpx[http2]` is a
dependency (rather than plain `httpx`) because it installs `h2`, which enables HTTP/2 connection
reuse for the shared client. `uvicorn` is only in the `dev` extra, for the load-test harness.

Excerpt of the demo output:

```text
== 1. Rate limit per caller + identity from the token
anonymous IP bucket: 100 ok, request 101 -> 429
429 carries CORS: https://app.acme.example, Retry-After=59s
body says tenant 'someone-else', token says 'acme' -> saved for: acme

== 4. Bounded LLM calls
primary fails, backup answers: hello world
deadline hit; provider calls per fallback: [1, 1, 0, 0]

== 8. Transcript by job id, deleted after delivery
broker message: {'job_id': 'job-1'}
worker took: very long meeting transcript | rows left: 0

== 9. Atomic counter: 8 threads x 100 increments
recorded: 800 of 800
```

## Usage guide

The full guide with every module is [docs/usage.md](docs/usage.md). The shortest useful tour:

```python
import asyncio

from fastapi_prod_hardening.bounded_llm import BoundedLLM, FakeProvider


async def main() -> None:
    llm = BoundedLLM([FakeProvider("primary", fail_times=1), FakeProvider("backup")])
    print(await llm.complete("summarise my tasks"))


asyncio.run(main())
```

```python
from fastapi.testclient import TestClient

from fastapi_prod_hardening.app import create_app
from fastapi_prod_hardening.identity import Hs256Verifier, Identity

token = Hs256Verifier("dev-secret").issue(Identity("acme", "alice"))
with TestClient(create_app(secret="dev-secret")) as client:
    reply = client.post("/v1/tasks", json={"title": "Plan sprint", "tenant_id": "someone-else"},
                        headers={"authorization": f"Bearer {token}"})
    print(reply.json())  # saved for tenant "acme", whatever the body said
    assert reply.json()["tenant_id"] == "acme"
```

Signatures are in [docs/api.md](docs/api.md).

## How it works

For a plain-English walk through each safeguard, see [How each part works, one by one](#how-each-part-works-one-by-one).

```mermaid
flowchart LR
    C[client] --> CORS[DynamicCORS<br/>in-memory allow-list]
    CORS --> RL[RateLimit<br/>caller bucket]
    RL --> ID[IdentityCheck<br/>token vs body]
    ID --> R{route}
    R -->|DB only: sync def| TP[thread pool<br/>shared limiter = DB pool size]
    R -->|mixed: async def| OFF[anyio.to_thread.run_sync]
    OFF --> TP
    R -->|LLM| B[BoundedLLM<br/>semaphore, deadline, breaker]
    B --> P[providers, tried once each]
    R -->|outgoing HTTP| H[HttpClients<br/>one client per loop]
    RL -.->|Lua INCR+EXPIRE| REDIS[(Redis or memory)]
    CORS -.->|reload every 60 s| DB[(origin table)]
    LAG[LoopLagSampler] --> M[/metrics/]
    TP --> POOL[Pool: records wait] --> M
```

A request passes the CORS layer first (so even a 429 carries CORS headers), then the limiter, which
picks a bucket from the service key and tokens, then the identity check, which compares the token's
tenant with any tenant named in the body. The route then runs on the right side of the event loop:
DB-only routes are plain `def` and run on the thread pool; mixed routes stay `async` and offload each
DB block through `anyio.to_thread.run_sync`. Both draw from one capacity limiter, sized to the DB
pool, so a request waits in a queue instead of waiting on the database connection pool.

An LLM call takes a slot in a per-loop semaphore, then tries each provider once, in order. Before
every attempt it asks the circuit breaker; an open circuit raises at once and sends nothing. The
240 s deadline is checked before each fallback.

## Design decisions

1. **Rate-limit per caller, not per IP.** Behind one gateway every request has the same source
   address. Rejected: removing the broken skip list and leaving an IP bucket, which would put the
   whole product in one 100/min bucket. The user token is trusted only together with a valid service
   key (constant-time compare); otherwise anyone could pick any bucket by sending a header.
2. **Skip lists are exact-match.** The limiter never ran because `"/"` sat in a prefix skip list and
   matched every path. Rejected: prefix matching.
3. **Fail open, with a cooldown.** A 250 ms Redis deadline, then a 30 s in-memory cooldown. Rejected:
   waiting on a hung Redis, which held every request for about 10 s.
4. **Identity from the token, measure first.** A measure mode logs body-versus-token mismatches
   before anything is enforced. Rejected: enforcing on day one, which could break clients nobody
   had measured.
5. **Redact at the handler.** A filter on a parent logger never sees records from child loggers.
   Rejected: a logger-level filter.
6. **Every LLM call is bounded.** The base is per-attempt timeouts and a circuit
   breaker. On top of that: the concurrency cap, the whole-call deadline, the stream caps and the
   call-time breaker check. Fallbacks never retry, so waits do not multiply.
7. **Per-loop state.** A semaphore or an HTTP client binds to the loop that first
   uses it, and a worker calling `asyncio.run()` repeatedly creates a new loop each time. Rejected:
   one global object.
8. **The stream cap applies to caller-facing streams only.** Every model streams internally, so a
   short cap on all streams would cut background work. Background streams get a 900 s backstop.
9. **Streaming uses the first healthy provider; there is no mid-stream fallback.** A provider that
   fails before its first token is skipped. After the first token the caller has seen output, and
   switching would splice two answers together. Rejected: buffering the whole stream to allow a
   switch, which defeats streaming.
10. **The HTTP client is a singleton and carries no tenant data.** Reuse avoids new connections
    and handshakes. Tenant and token headers are arguments to each call, so one tenant's credentials
    can never leak into another's request.
11. **Sync `def` for DB-only routes, `to_thread` for mixed ones, not `AsyncSession`.** `AsyncSession`
    means rewriting every call site and a second connection pool against a database with a hard
    connection limit. One shared limiter, sized to the DB pool, covers both paths. Offloads must use
    `anyio.to_thread` (not `asyncio.to_thread`), or they draw from a different budget.
12. **Measure first.** The loop-lag sampler and the pool-wait histogram came before the refactor,
    with a `/health` canary in the load test, so the improvement could be shown and not assumed.
13. **Large payloads go through the database, by job id.** A broker copies a message into its queue,
    retries, task UIs and log indexes. A database row has one copy, tenant scoping and a one-statement
    delete. Rejected: object storage in the request path.
14. **Atomic counters use one statement.** Read-add-write in application code loses updates under
    concurrency; `UPDATE ... RETURNING` does not.
15. **CORS from data, never per request.** An in-memory list reloaded every 60 s; if the database is
    down the last good list stays. Empty table means no origins allowed, so rolling it out changes
    nothing until rows are added.
16. **CI: change-aware, with gates that cannot pass by not running.** See
    [docs/change-aware-ci.md](docs/change-aware-ci.md). A report that is missing, empty or all
    skipped fails; DB suites fail on any skip; the migration graph is checked without importing app code.

## Testing

```bash
.venv/bin/pytest -q            # 100 tests, about 10 s, no network
.venv/bin/ruff check .
```

| Invariant | Test |
|---|---|
| A `"/"` skip entry would bypass the limiter | `test_root_prefix_skip_list_would_bypass_everything` |
| User bucket needs a valid service key | `test_user_bucket_needs_valid_service_key` |
| 429 carries CORS headers and `Retry-After` | `test_429_has_cors_and_retry_after` |
| Redis failure fails open with a cooldown | `test_redis_failure_fails_open_with_cooldown` |
| Identity comes from the token, not the body | `test_identity_from_token_not_body` |
| Redaction works for child loggers; reinstall after a handler swap; scrubbing is idempotent | `test_child_logger_record_redacted_at_handler`, `test_reinstall_after_framework_replaces_handlers`, `test_scrubbing_twice_equals_scrubbing_once` |
| Deadline stops fallbacks; open circuit sends nothing | `test_deadline_stops_new_fallbacks`, `test_open_circuit_fails_fast_with_zero_calls` |
| Streams use the first healthy provider, no mid-stream fallback | `test_stream_uses_first_healthy_provider`, `test_stream_does_not_fall_back_after_first_token` |
| One client per loop; tenant headers never stored on the client | `test_one_client_per_event_loop`, `test_tenant_headers_are_per_request_never_stored_on_the_client` |
| Transcript deleted after delivery; 24 h sweep; 50 MB limit; names-only task log | `tests/test_transcripts.py` |
| Sync routes and offloads share one thread budget | `test_sync_routes_and_offloads_share_one_thread_budget` |
| Loop lag sees a blocked loop; pool wait is recorded | `tests/test_metrics.py` |
| Atomic counter loses no updates under 8 threads | `test_atomic_counter_never_loses_updates_under_concurrency` |
| JUnit gate: missing, empty, all-skipped | `tests/test_ci_gates.py` |
| `--strict-skips` fails on a skip | `test_strict_skips_fails_the_run_when_a_test_is_skipped` |
| Migration graph: duplicates, missing parent, cycle, multiple heads, no import | `tests/test_ci_gates.py` |
| Every code block in these docs runs | `tests/test_docs.py` |

## Limits & known trade-offs

- `MemoryBackend` is per process: N workers allow N times the limit. Use `RedisBackend` when shared.
- Streams use the first healthy provider and never switch mid-stream.
- `HttpClients.aclose()` closes only the running loop's client; other loops' clients close with their loops.
- `/metrics` is unauthenticated and the metrics are in process (per worker); a real deployment would use Prometheus.
- `Hs256Verifier` is a stand-in, not a full JWT implementation.
- The CI files other than `ci.yml` are templates; they are validated for shape but not run here.
- `scripts/loadtest.py` is a harness, not a benchmark: numbers depend on your machine and are not CI-gated.
- SQLite serialises writers, so the atomic counter test proves correctness, not Postgres-scale throughput.
- Not here: connection budgets per service role, advisory-lock migrations, self-healing background
  sweeps, billing and review workflows.

## Project layout

```text
src/fastapi_prod_hardening/
  app.py           example API wiring everything together (sync, mixed and metrics routes)
  ratelimit.py     per-caller buckets, Redis Lua backend, fail-open limiter and middleware
  identity.py      token verification, identity dependency, measure/enforce check
  redaction.py     handler-level log scrubbing
  bounded_llm.py   concurrency cap, deadline, breaker, stream caps, FakeProvider
  http.py          shared per-loop httpx client with per-request headers
  transcripts.py   job-id payload store, sweep, names-only task log
  db.py            bounded connection pool that records wait time
  counters.py      atomic UPDATE ... RETURNING counter
  metrics.py       loop-lag sampler, histograms, /metrics text
  threads.py       one shared anyio thread limiter
  cors.py          database allow-list, background reload, admin validation
  strict_skips.py  pytest plugin: --strict-skips
scripts/           check_junit.py, check_migrations.py, loadtest.py
tests/             one file per module, fixtures/ for migration folders, test_docs.py
examples/demo.py   ten-section offline walkthrough
docs/              usage.md, api.md, change-aware-ci.md
.github/workflows/ ci.yml and the change-aware and DB-suite templates
```
