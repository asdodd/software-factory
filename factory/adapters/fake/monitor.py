from __future__ import annotations

from collections.abc import Mapping

from factory.ports.context import AdapterContext
from factory.ports.types import Cursor, Finding, Page


class FakeMonitor:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> FakeMonitor:
        return cls()

    def poll_findings(self, cursor: Cursor | None = None) -> Page[Finding]:
        start = int(cursor or 0)
        return Page(tuple(self.findings[start:]), str(len(self.findings)))
