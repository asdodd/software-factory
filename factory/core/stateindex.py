"""Small JSON records and per-agent indexes on top of the StateStore port."""

from __future__ import annotations

import json


class Records:
    def __init__(self, state, namespace: str) -> None:
        self.state = state
        self.ns = namespace

    def get(self, key: str) -> dict | None:
        raw = self.state.get(self.ns, key)
        return json.loads(raw) if raw else None

    def put(self, key: str, value: dict) -> None:
        self.state.put(self.ns, key, json.dumps(value, sort_keys=True))

    def delete(self, key: str) -> None:
        self.state.delete(self.ns, key)


class Index:
    """A set of keys per agent, updated with compare-and-set so concurrent workers do not lose entries."""

    def __init__(self, state, namespace: str, agent: str) -> None:
        self.state = state
        self.ns = namespace
        self.agent = agent

    def members(self) -> list[str]:
        return json.loads(self.state.get(self.ns, self.agent) or "[]")

    def _update(self, fn) -> None:
        while True:
            raw = self.state.get(self.ns, self.agent)
            new = json.dumps(sorted(fn(set(json.loads(raw or "[]")))))
            if self.state.compare_and_set(self.ns, self.agent, raw, new):
                return

    def add(self, key: str) -> None:
        self._update(lambda s: s | {key})

    def remove(self, key: str) -> None:
        self._update(lambda s: s - {key})
