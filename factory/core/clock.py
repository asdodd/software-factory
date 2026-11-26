from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from factory.ports.context import AdapterContext


class SystemClock:
    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> SystemClock:
        return cls()

    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidIdSource:
    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> UuidIdSource:
        return cls()

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"
