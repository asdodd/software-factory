from __future__ import annotations

from typing import Protocol, runtime_checkable

from factory.ports.access_guard import AccessGuard
from factory.ports.types import Command, CommandResult, HarnessResult, HarnessTask


@runtime_checkable
class HarnessRuntime(Protocol):
    """What the factory lends a harness for one task.

    run: execute argv in the task's sandbox (cwd relative to the workspace).
    guard: the per-run access guard; the builtin harness routes every tool
      call through it. External harnesses cannot be made to, which is why
      their workspace is isolated and their diff is checked afterwards.
    max_denials: denied attempts after which a harness must stop."""

    guard: AccessGuard
    max_denials: int

    def run(self, command: Command) -> CommandResult: ...


@runtime_checkable
class AgentHarness(Protocol):
    """The inner agent loop for a stage. Bring your own: the built-in tool loop,
    or any CLI agent wrapped by the command adapter.

    A harness only edits files in task.workspace and talks to task.endpoint.
    Claiming, pushing, comments, gates, and trace upload stay in the factory,
    so every harness goes through the same shared process."""

    def run(self, task: HarnessTask, runtime: HarnessRuntime) -> HarnessResult: ...
