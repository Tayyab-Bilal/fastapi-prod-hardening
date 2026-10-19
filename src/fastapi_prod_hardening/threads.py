"""One thread budget for sync routes and offloads, sized to the DB pool."""

from __future__ import annotations

import anyio.to_thread
from anyio import CapacityLimiter


def shared_limiter(total: int = 30) -> CapacityLimiter:
    """Resize anyio's default limiter, the one FastAPI's sync routes already use.

    Offloads must go through ``anyio.to_thread.run_sync`` (not ``asyncio.to_thread``, which has its
    own executor) so both paths draw from the same budget. Call inside the running loop (lifespan);
    the limiter is per event loop.
    """
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = total
    return limiter
