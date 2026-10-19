"""Redact at the handler, not the logger.

A filter attached to a *logger* only sees records created by that logger. Records from child loggers
propagate straight to the parent's *handlers* and skip the parent's filters, so secrets leak. The
handler's formatter sees every record that is actually written, so scrub there.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

_PATTERNS = [
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+(@)", re.I), r"\1***\2"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer ***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***"),
]


class SecretScrubFormatter(logging.Formatter):
    def __init__(self, fmt: str | None = None, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self.secrets = [s for s in secrets if s]

    def scrub(self, text: str) -> str:
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def format(self, record: logging.LogRecord) -> str:
        # Scrub the final string so exception text and %-args are covered too.
        return self.scrub(super().format(record))


def install_redaction(root: logging.Logger | None = None, secrets: Iterable[str] = ()) -> None:
    """Attach to every handler. Re-run after a framework replaces the handlers."""
    root = root or logging.getLogger()
    for handler in root.handlers:
        old = handler.formatter
        fmt = getattr(old, "_fmt", None)
        handler.setFormatter(SecretScrubFormatter(fmt, secrets))
