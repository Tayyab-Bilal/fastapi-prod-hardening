"""Tiny load generator with a /health canary. Not run in CI; see docs/usage.md.

It sends N concurrent clients at one path while a canary polls /health, then prints canary and
load latency. If a handler blocks the event loop, the canary slows down with it: that is the
symptom the original investigation measured first, before changing any code.

    uvicorn --factory fastapi_prod_hardening.app:load_test_app &
    python scripts/loadtest.py http://127.0.0.1:8000 /debug/blocks-loop
    python scripts/loadtest.py http://127.0.0.1:8000 /debug/threadpool
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))] if ordered else 0.0


async def run_load(client: httpx.AsyncClient, path: str, *, clients: int = 20,
                   seconds: float = 3.0) -> dict[str, float]:
    """Hit ``path`` from ``clients`` workers for ``seconds`` while sampling /health."""
    stop = time.perf_counter() + seconds
    load: list[float] = []
    canary: list[float] = []

    async def worker() -> None:
        while time.perf_counter() < stop:
            start = time.perf_counter()
            await client.get(path)
            load.append(time.perf_counter() - start)

    async def poll_health() -> None:
        while time.perf_counter() < stop:
            start = time.perf_counter()
            await client.get("/health")
            canary.append(time.perf_counter() - start)
            await asyncio.sleep(0.02)

    await asyncio.gather(poll_health(), *(worker() for _ in range(clients)))
    return {"requests": len(load), "req_per_min": len(load) / seconds * 60,
            "load_p50_ms": statistics.median(load) * 1000,
            "canary_p50_ms": statistics.median(canary) * 1000,
            "canary_max_ms": max(canary) * 1000}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("path")
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        for key, value in (await run_load(client, args.path, clients=args.clients,
                                          seconds=args.seconds)).items():
            print(f"{key:>14}: {value:,.1f}")


if __name__ == "__main__":
    asyncio.run(main())
