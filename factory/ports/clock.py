from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC."""
        ...


@runtime_checkable
class IdSource(Protocol):
    def new_id(self, prefix: str) -> str: ...
