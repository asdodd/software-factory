from __future__ import annotations

from typing import Protocol, runtime_checkable

from factory.ports.types import HarnessResult, HarnessTask


@runtime_checkable
class AgentHarness(Protocol):
    """The inner agent loop for a stage. Bring your own: the built-in tool loop,
    or any CLI agent wrapped by the command adapter.

    A harness only edits files in task.workspace and talks to task.endpoint.
    Claiming, pushing, comments, gates, and trace upload stay in the factory,
    so every harness goes through the same shared process."""

    def run(self, task: HarnessTask) -> HarnessResult: ...
