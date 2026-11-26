from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from factory.ports.types import Artifact, Command, CommandResult, Workspace, WorkspaceSpec


@runtime_checkable
class Sandbox(Protocol):
    """Isolated execution. The agent never calls this directly: its tools go
    through the AccessGuard first."""

    def prepare(self, spec: WorkspaceSpec) -> Workspace: ...

    def run(self, workspace: Workspace, command: Command) -> CommandResult:
        """argv only, never a shell string."""
        ...

    def collect_artifacts(self, workspace: Workspace, patterns: Sequence[str]) -> tuple[Artifact, ...]: ...

    def teardown(self, workspace: Workspace) -> None: ...
