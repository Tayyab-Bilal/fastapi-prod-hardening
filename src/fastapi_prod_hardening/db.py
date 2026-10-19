"""A tiny bounded connection pool (SQLite stands in for Postgres) that records how long callers wait."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from typing import Iterator

from .metrics import Histogram


class Pool:
    """At most ``size`` connections out at once. Size it to match the shared thread limiter.

    If more threads than ``size`` can run DB code, the extra ones wait here instead of in the
    queue, which looks like a slow database. Recording the wait makes that visible.
    """

    def __init__(self, path: str, size: int = 30, wait_histogram: Histogram | None = None) -> None:
        self.path, self.size, self.wait = path, size, wait_histogram
        self._slots = threading.BoundedSemaphore(size)

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        start = time.perf_counter()
        self._slots.acquire()
        if self.wait:
            self.wait.observe(time.perf_counter() - start)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:  # commit on success, roll back on error
                yield conn
        finally:
            conn.close()
            self._slots.release()
