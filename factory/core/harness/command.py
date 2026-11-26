"""Bring-your-own harness: run any CLI agent in the sandbox.

Settings:
  argv: list of strings with placeholders {workspace} {instructions_file}
        {result_file} {gateway_url} {gateway_token} {model} {max_turns}
  env: table of variables (same placeholders), e.g. the CLI's base-URL and key variables
  stdin_file: optional placeholder string; that file is fed to stdin
The CLI may write {result_file} as JSON {"status", "summary", "questions"}.
Otherwise exit code 0 means done and the tail of stdout is the summary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from factory.ports.context import AdapterContext
from factory.ports.harness import HarnessRuntime
from factory.ports.types import Command, HarnessResult, HarnessStatus, HarnessTask

PLACEHOLDERS = ("workspace", "instructions_file", "result_file", "gateway_url", "gateway_token", "model", "max_turns")


class CommandHarness:
    def __init__(self, argv: list[str], env: Mapping[str, str], stdin_file: str = "") -> None:
        if not argv:
            raise ValueError("harness.command needs settings.argv")
        self.argv = argv
        self.env = dict(env)
        self.stdin_file = stdin_file

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> CommandHarness:
        return cls([str(a) for a in settings.get("argv", [])],
                   {str(k): str(v) for k, v in dict(settings.get("env", {})).items()},
                   str(settings.get("stdin_file", "")))

    def run(self, task: HarnessTask, runtime: HarnessRuntime) -> HarnessResult:
        values = {
            "workspace": str(task.workspace.root), "instructions_file": str(task.instructions_file),
            "result_file": str(task.result_file), "gateway_url": task.endpoint.base_url,
            "gateway_token": task.endpoint.token, "model": task.endpoint.model, "max_turns": str(task.max_turns),
        }

        def fill(text: str) -> str:
            for name in PLACEHOLDERS:
                text = text.replace("{" + name + "}", values[name])
            return text

        command = Command(tuple(fill(a) for a in self.argv), ".", {k: fill(v) for k, v in self.env.items()},
                          task.timeout_seconds, fill(self.stdin_file) if self.stdin_file else "")
        result = runtime.run(command)
        guard = runtime.guard
        result_file = Path(task.result_file)
        if result_file.is_file():
            try:
                doc = json.loads(result_file.read_text())
                return HarnessResult(HarnessStatus(doc.get("status", "done")),
                                     guard.filter_output(str(doc.get("summary", ""))).text,
                                     tuple(guard.filter_output(str(q)).text for q in doc.get("questions", ())))
            except (json.JSONDecodeError, ValueError):
                return HarnessResult(HarnessStatus.FAILED, "harness wrote an unreadable result file")
        if result.timed_out:
            return HarnessResult(HarnessStatus.TIMED_OUT, f"harness timed out after {task.timeout_seconds:.0f}s")
        summary = guard.filter_output(result.stdout[-2000:]).text
        return HarnessResult(HarnessStatus.DONE if result.exit_code == 0 else HarnessStatus.FAILED, summary)
