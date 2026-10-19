import threading
import time

import anyio.to_thread

from fastapi_prod_hardening.threads import shared_limiter


async def test_shared_limiter_bounds_concurrency():
    shared_limiter(3)
    running = peak = 0
    lock = threading.Lock()

    def work():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1

    async with anyio.create_task_group() as tg:
        for _ in range(12):
            tg.start_soon(anyio.to_thread.run_sync, work)
    assert 1 < peak <= 3  # extra offloads queued instead of running at once
