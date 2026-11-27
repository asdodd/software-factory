from __future__ import annotations

from typing import Protocol, runtime_checkable

from factory.ports.types import Command, CommandResult, HarnessResult, HarnessTask


@runtime_checkable
class HarnessRuntime(Protocol):
    """What the factory lends a harness for one task.

    run: execute argv in the task's sandbox (cwd relative to the workspace)."""

    def run(self, command: Command) -> CommandResult: ...


@runtime_checkable
class AgentHarness(Protocol):
    """The inner agent loop for a stage: any CLI agent (Claude Code, Codex,
    Cline, or one wrapped by the command adapter).

    A harness only edits files in task.workspace and talks to task.endpoint.
    The workspace carries the agent kit (restricted-files skill and hooks), so
    the agent's own tools enforce it. Claiming, pushing, comments, gates, and
    trace upload stay in the factory, so every harness goes through the same
    shared process."""

    def run(self, task: HarnessTask, runtime: HarnessRuntime) -> HarnessResult: ...
