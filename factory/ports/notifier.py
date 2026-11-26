from __future__ import annotations

from typing import Protocol, runtime_checkable

from factory.ports.types import Notification


@runtime_checkable
class Notifier(Protocol):
    def notify(self, notification: Notification) -> None: ...
