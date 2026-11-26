from __future__ import annotations

from collections.abc import Mapping

from factory.ports.context import AdapterContext
from factory.ports.types import Notification


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> FakeNotifier:
        return cls()

    def notify(self, notification: Notification) -> None:
        self.sent.append(notification)
