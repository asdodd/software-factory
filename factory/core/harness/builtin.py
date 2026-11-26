"""Builtin harness: a small tool loop over the loopback gateway. Every tool
call goes through the AccessGuard, and command output is filtered."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from factory.core.harness.client import GatewayClient, GatewayError
from factory.ports.context import AdapterContext
from factory.ports.harness import HarnessRuntime
from factory.ports.types import Command, HarnessResult, HarnessStatus, HarnessTask

MAX_READ = 200_000
MAX_RESULTS = 200

TOOLS = [
    {"name": "read_file", "description": "Read a text file in the workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_dir", "description": "List a directory in the workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "search", "description": "Search files for a regular expression.",
     "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                    "required": ["pattern"]}},
    {"name": "write_file", "description": "Create or overwrite a file in the workspace.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    {"name": "run_command", "description": "Run a command (argv list, no shell) in the workspace.",
     "parameters": {"type": "object", "properties": {"argv": {"type": "array", "items": {"type": "string"}},
                                                     "cwd": {"type": "string"}}, "required": ["argv"]}},
    {"name": "finish", "description": "Finish the task.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["done", "needs_info", "blocked", "failed"]},
         "summary": {"type": "string"}, "questions": {"type": "array", "items": {"type": "string"}}},
         "required": ["status", "summary"]}},
]


class BuiltinHarness:
    def __init__(self, timeout_seconds: float = 300.0) -> None:
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> BuiltinHarness:
        return cls(float(ctx.config.gateway.request_timeout_seconds))

    def run(self, task: HarnessTask, runtime: HarnessRuntime) -> HarnessResult:
        client = GatewayClient(task.endpoint.base_url, task.endpoint.token, self.timeout_seconds)
        root = task.workspace.root
        guard = runtime.guard
        start_denials = len(getattr(guard, "denials", []))
        messages: list[dict] = [
            {"role": "system", "content": Path(task.instructions_file).read_text()},
            {"role": "user", "content": "Begin. Use the tools, then call finish."},
        ]
        tools = [{"type": "function", "function": t} for t in TOOLS]
        for _turn in range(task.max_turns):
            try:
                reply = client.chat({"model": task.endpoint.model, "messages": messages, "tools": tools})
            except GatewayError as exc:
                status = HarnessStatus.FAILED
                return HarnessResult(status, "model budget exceeded" if exc.status == 429 else str(exc))
            msg = reply["choices"][0]["message"]
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                return _final(msg.get("content") or "")
            for call in calls:
                name = call["function"]["name"]
                args = json.loads(call["function"].get("arguments") or "{}")
                if name == "finish":
                    return HarnessResult(HarnessStatus(args.get("status", "done")),
                                         guard.filter_output(str(args.get("summary", ""))).text,
                                         tuple(guard.filter_output(str(q)).text for q in args.get("questions", ())))
                output = self._tool(name, args, root, runtime)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
                denied = len(getattr(guard, "denials", [])) - start_denials
                if denied >= runtime.max_denials:
                    return HarnessResult(HarnessStatus.BLOCKED,
                                         f"stopped after {denied} denied access attempts")
        return HarnessResult(HarnessStatus.FAILED, f"no result after {task.max_turns} turns")

    def _tool(self, name: str, args: Mapping[str, object], root: Path, runtime: HarnessRuntime) -> str:
        guard = runtime.guard
        if name in ("read_file", "list_dir", "write_file", "search"):
            path = str(args.get("path") or ".")
            decision = guard.check_path(root, path)
            if not decision.allowed:
                return f"denied: {decision.reason}"
            target = (root / path).resolve()
            if name == "read_file":
                if not target.is_file():
                    return "error: not a file"
                return guard.filter_output(target.read_text(errors="replace")[:MAX_READ]).text
            if name == "list_dir":
                if not target.is_dir():
                    return "error: not a directory"
                entries = []
                for child in sorted(target.iterdir()):
                    rel = os.path.relpath(child, root.resolve())
                    if guard.is_visible(root, rel):
                        entries.append(child.name + ("/" if child.is_dir() else ""))
                return "\n".join(entries)
            if name == "write_file":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(args.get("content", "")))
                return "ok"
            return self._search(str(args.get("pattern", "")), target, root, guard)
        if name == "run_command":
            argv = [str(a) for a in args.get("argv") or []]
            decision = guard.check_command(root, argv)
            if not decision.allowed:
                return f"denied: {decision.reason}"
            result = runtime.run(Command(tuple(argv), str(args.get("cwd") or ".")))
            text = f"exit {result.exit_code}\n{result.stdout}\n{result.stderr}"
            return guard.filter_output(text[-MAX_READ:]).text
        return f"error: unknown tool {name}"

    def _search(self, pattern: str, target: Path, root: Path, guard) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"error: bad pattern: {exc}"
        hits = []
        files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
        for path in files:
            rel = os.path.relpath(path, root.resolve())
            if path.is_symlink() or not guard.is_visible(root, rel) or not guard.check_path(root, rel).allowed:
                continue
            for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{n}: {line[:300]}")
                    if len(hits) >= MAX_RESULTS:
                        break
        return guard.filter_output("\n".join(hits)).text or "no matches"


def _final(content: str) -> HarnessResult:
    try:
        doc = json.loads(content)
        return HarnessResult(HarnessStatus(doc.get("status", "done")), str(doc.get("summary", "")),
                             tuple(doc.get("questions", ())))
    except (json.JSONDecodeError, ValueError, AttributeError):
        return HarnessResult(HarnessStatus.DONE, content[-2000:])
