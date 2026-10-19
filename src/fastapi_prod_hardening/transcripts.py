"""Keep large payloads out of the message broker: pass a job id, store the body in the database."""

from __future__ import annotations

import contextlib
import inspect
import logging
import sqlite3
import time
from typing import Any, Callable

MAX_BYTES = 50 * 1024 * 1024
SWEEP_AFTER_S = 24 * 3600


class PayloadTooLarge(ValueError):
    pass


class TranscriptStore:
    """Transcripts travel by job id.

    A broker copies the whole message into its queue, its retry state, task UIs and log indexes.
    A database row keeps the text in one place that has tenant scoping and a one-statement delete.
    The task message carries only the job id; the worker calls ``take``, which returns the text and
    deletes it in the same statement, so delivery and cleanup cannot drift apart. ``sweep`` removes
    rows that a killed worker never took.
    """

    def __init__(self, path: str, clock: Callable[[], float] = time.time,
                 max_bytes: int = MAX_BYTES) -> None:
        self.path, self.clock, self.max_bytes = path, clock, max_bytes
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS transcripts ("
                      "job_id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at REAL NOT NULL)")

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def put(self, job_id: str, text: str) -> None:
        if len(text.encode()) > self.max_bytes:
            raise PayloadTooLarge(f"transcript over {self.max_bytes} bytes")
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO transcripts VALUES (?, ?, ?)",
                      (job_id, text, self.clock()))

    def take(self, job_id: str) -> str | None:
        """Return the transcript and delete it, or None if it is gone (already delivered/swept)."""
        with self._conn() as c:
            row = c.execute("DELETE FROM transcripts WHERE job_id = ? RETURNING body",
                            (job_id,)).fetchone()
        return row[0] if row else None

    def sweep(self, older_than_s: float = SWEEP_AFTER_S) -> int:
        """Delete leftovers from killed workers; returns how many rows were removed."""
        with self._conn() as c:
            return c.execute("DELETE FROM transcripts WHERE created_at < ?",
                             (self.clock() - older_than_s,)).rowcount

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]


def log_task_start(logger: logging.Logger, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """Log which arguments a task got, never their values (values may hold personal data)."""
    names = list(inspect.signature(fn).bind(*args, **kwargs).arguments)
    line = f"task {fn.__name__} started with arguments: {', '.join(names)}"
    logger.info(line)
    return line
