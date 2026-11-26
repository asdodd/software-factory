"""In-process metrics. Exporters are a later concern; counters are enough to test."""

from __future__ import annotations

import threading
from collections import Counter


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._timings: dict[str, list[float]] = {}

    def incr(self, name: str, n: int = 1, **labels: str) -> None:
        with self._lock:
            self._counters[_key(name, labels)] += n

    def observe(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._timings.setdefault(_key(name, labels), []).append(value)

    def count(self, name: str, **labels: str) -> int:
        with self._lock:
            if labels:
                return self._counters[_key(name, labels)]
            return sum(v for k, v in self._counters.items() if k == name or k.startswith(name + "{"))

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    return name + "{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"
