"""Redaction shared by logging, config display, comments, and traces.

Counts are reported; redacted content never is.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass

BUILTIN_SECRET_PATTERNS: tuple[str, ...] = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}",
    r"(?i)\b(?:api[_-]?key|token|secret|password|passwd)\b\s*[:=]\s*['\"]?[^\s'\"]{6,}",
    r"\bglpat-[A-Za-z0-9_-]{16,}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
)

SECRET_PLACEHOLDER = "[REDACTED]"


@dataclass(frozen=True)
class Redacted:
    text: str
    count: int


class Redactor:
    """Thread-safe. Known secret values (resolved at runtime) are always scrubbed."""

    def __init__(self, extra_patterns: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()
        self._patterns = [re.compile(p) for p in (*BUILTIN_SECRET_PATTERNS, *extra_patterns)]

    def add_secret(self, value: str) -> None:
        if value and len(value) >= 4:
            with self._lock:
                self._values.add(value)

    def redact(self, text: str) -> Redacted:
        count = 0
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                count += text.count(value)
                text = text.replace(value, SECRET_PLACEHOLDER)
        for pattern in self._patterns:
            text, n = pattern.subn(SECRET_PLACEHOLDER, text)
            count += n
        return Redacted(text, count)

    def redact_obj(self, obj: object) -> tuple[object, int]:
        if isinstance(obj, str):
            r = self.redact(obj)
            return r.text, r.count
        if isinstance(obj, dict):
            total = 0
            out = {}
            for k, v in obj.items():
                out[k], n = self.redact_obj(v)
                total += n
            return out, total
        if isinstance(obj, (list, tuple)):
            total = 0
            items = []
            for v in obj:
                item, n = self.redact_obj(v)
                items.append(item)
                total += n
            return items, total
        return obj, 0
