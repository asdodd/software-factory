from __future__ import annotations

from typing import Protocol, runtime_checkable

from factory.ports.types import Cursor, Finding, Page


@runtime_checkable
class Monitor(Protocol):
    """Regression and production monitors. Findings become intake events."""

    def poll_findings(self, cursor: Cursor | None = None) -> Page[Finding]: ...
