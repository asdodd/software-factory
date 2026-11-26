from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol, runtime_checkable

from factory.ports.types import Artifact, OutcomeRecord, RunMeta, TraceEvent, TraceQuery


@runtime_checkable
class TraceWriter(Protocol):
    """What agents get. Write-only: no read-back, no delete."""

    def put_run(self, meta: RunMeta, events: Sequence[TraceEvent], artifacts: Sequence[Artifact]) -> None:
        """Buffered; failures queue for retry and never raise into the pipeline."""
        ...

    def append_outcome(self, record: OutcomeRecord) -> None: ...

    def flush(self) -> None: ...


@runtime_checkable
class TraceReader(Protocol):
    """What grading, self-improvement, and humans get, with separate credentials."""

    def get(self, run_id: str) -> tuple[RunMeta, tuple[TraceEvent, ...], tuple[OutcomeRecord, ...]]: ...

    def list(self, query: TraceQuery) -> Iterator[RunMeta]:
        """Uses per-day manifests; never lists the whole bucket."""
        ...


@runtime_checkable
class TraceStore(TraceWriter, TraceReader, Protocol):
    pass
