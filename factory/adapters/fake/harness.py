"""A bring-your-own harness stand-in. It acts on the workspace directly, as an
external CLI agent would, without the factory's guarded tools. Scenarios use
it to prove the isolation and diff-back gates hold for harnesses the factory
did not write."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from factory.ports.context import AdapterContext
from factory.ports.harness import HarnessRuntime
from factory.ports.types import HarnessResult, HarnessStatus, HarnessTask


class ScriptedHarness:
    def __init__(self) -> None:
        self.scripts: dict[str, list[list[Mapping[str, object]]]] = {}
        self.tasks: list[HarnessTask] = []
        self.observations: list[dict[str, object]] = []

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> ScriptedHarness:
        return cls()

    def queue(self, ticket: str, actions: Sequence[Mapping[str, object]]) -> None:
        self.scripts.setdefault(ticket, []).append(list(actions))

    def run(self, task: HarnessTask, runtime: HarnessRuntime) -> HarnessResult:
        self.tasks.append(task)
        queue = self.scripts.get(task.ticket_key) or [[{"result": {"status": "done", "summary": "no-op"}}]]
        actions = queue.pop(0) if len(queue) > 1 else queue[0]
        root = task.workspace.root
        result = HarnessResult(HarnessStatus.DONE, "scripted harness finished")
        for action in actions:
            if "write" in action:
                target = root / str(action["write"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(action.get("content", "")))
            elif "delete" in action:
                (root / str(action["delete"])).unlink(missing_ok=True)
            elif "observe" in action:
                files = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                               if p.is_file() and ".git" not in p.relative_to(root).parts)
                history = subprocess.run(["git", "log", "-p", "--all"], cwd=root, capture_output=True, text=True,
                                         env={"PATH": os.environ.get("PATH", "")}).stdout
                instructions = Path(task.instructions_file).read_text()
                self.observations.append({"files": files, "history": history, "instructions": instructions,
                                          "env_keys": sorted(os.environ)})
            elif "result" in action:
                raw = dict(action["result"])
                Path(task.result_file).write_text(json.dumps(raw))
                result = HarnessResult(HarnessStatus(raw.get("status", "done")), str(raw.get("summary", "")),
                                       tuple(raw.get("questions", ())))
        return result
