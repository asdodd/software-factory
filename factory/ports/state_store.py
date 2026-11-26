from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StateStore(Protocol):
    """Small durable key-value store for cursors, idempotency keys, and paused
    runs. Not in the spec's port list; proposed in the Phase 0 report.
    Real implementation: sqlite3 (stdlib)."""

    def get(self, namespace: str, key: str) -> str | None: ...

    def put(self, namespace: str, key: str, value: str) -> None: ...

    def compare_and_set(self, namespace: str, key: str, expected: str | None, value: str) -> bool: ...

    def delete(self, namespace: str, key: str) -> None: ...
