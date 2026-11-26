from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from factory.ports.context import AdapterContext


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 5, 9, 0, tzinfo=UTC)

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> FakeClock:
        start = settings.get("start")
        return cls(datetime.fromisoformat(str(start)) if start else None)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept = getattr(self, "slept", 0.0) + max(0.0, seconds)
        self._now += timedelta(seconds=max(0.0, seconds))

    def advance(self, **delta: float) -> None:
        self._now += timedelta(**delta)

    def set(self, when: datetime) -> None:
        self._now = when


class SequentialIdSource:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> SequentialIdSource:
        return cls()

    def new_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}-{n:04d}"
