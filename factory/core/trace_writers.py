"""Trace writer views handed to agents."""

from __future__ import annotations

from collections.abc import Sequence

from factory.ports.types import Artifact, OutcomeRecord, RunMeta, TraceEvent


class NullTraceWriter:
    def put_run(self, meta: RunMeta, events: Sequence[TraceEvent], artifacts: Sequence[Artifact]) -> None:
        pass

    def append_outcome(self, record: OutcomeRecord) -> None:
        pass

    def flush(self) -> None:
        pass


class WriteOnlyTraceWriter:
    """What agents receive: put, append, flush. No get, list, or delete."""

    __slots__ = ("_put_run", "_append_outcome", "_flush")

    def __init__(self, store) -> None:
        self._put_run = store.put_run
        self._append_outcome = store.append_outcome
        self._flush = store.flush

    def put_run(self, meta: RunMeta, events: Sequence[TraceEvent], artifacts: Sequence[Artifact]) -> None:
        self._put_run(meta, events, artifacts)

    def append_outcome(self, record: OutcomeRecord) -> None:
        self._append_outcome(record)

    def flush(self) -> None:
        self._flush()


def write_only(store) -> WriteOnlyTraceWriter:
    return WriteOnlyTraceWriter(store)
