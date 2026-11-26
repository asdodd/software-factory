from __future__ import annotations

import threading
from collections.abc import Mapping

from factory.ports.context import AdapterContext


class MemoryStateStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> MemoryStateStore:
        return cls()

    def get(self, namespace: str, key: str) -> str | None:
        with self._lock:
            return self._data.get((namespace, key))

    def put(self, namespace: str, key: str, value: str) -> None:
        with self._lock:
            self._data[(namespace, key)] = value

    def compare_and_set(self, namespace: str, key: str, expected: str | None, value: str) -> bool:
        with self._lock:
            if self._data.get((namespace, key)) != expected:
                return False
            self._data[(namespace, key)] = value
            return True

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            self._data.pop((namespace, key), None)
