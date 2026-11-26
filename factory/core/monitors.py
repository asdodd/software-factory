"""Monitor that runs a configured command (a regression suite, a log scanner,
a health probe) and reads findings from its stdout as JSON lines:

  {"id": "...", "summary": "...", "details": "...", "repo": "...", "location": "...", "severity": "high"}

Each line becomes a Finding. Repeated findings are harmless: intake keys on the
finding id and dedupes by fingerprint. The command runs with a scrubbed
environment and a timeout, from a fixed argv (no shell)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from collections.abc import Mapping

from factory.ports.context import AdapterContext
from factory.ports.types import Cursor, Finding, Page

log = logging.getLogger("factory.monitor")


class CommandMonitor:
    def __init__(self, name: str, argv: list[str], timeout_seconds: float = 300.0,
                 env: Mapping[str, str] | None = None, cwd: str | None = None) -> None:
        if not argv:
            raise ValueError("monitor.command needs settings.argv")
        self.name = name
        self.argv = argv
        self.timeout = timeout_seconds
        self.env = dict(env or {})
        self.cwd = cwd

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> CommandMonitor:
        return cls(str(settings.get("name", "command")), [str(a) for a in settings.get("argv", [])],
                   float(settings.get("timeout_seconds", 300)),
                   {str(k): str(v) for k, v in dict(settings.get("env", {})).items()},
                   str(ctx.base_dir / str(settings["cwd"])) if settings.get("cwd") else None)

    def poll_findings(self, cursor: Cursor | None = None) -> Page[Finding]:
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
        env.update(self.env)
        try:
            proc = subprocess.run(self.argv, capture_output=True, text=True, timeout=self.timeout, env=env,
                                  cwd=self.cwd, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("monitor %s did not run: %s", self.name, type(exc).__name__)
            return Page((), cursor)
        findings = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            summary = str(d.get("summary", "")).strip()
            if not summary:
                continue
            fid = str(d.get("id") or hashlib.sha256(line.encode()).hexdigest()[:16])
            findings.append(Finding(fid, self.name, str(d.get("repo", "")), str(d.get("location", "")), summary,
                                    str(d.get("details", "")), str(d.get("severity", "medium"))))
        return Page(tuple(findings), cursor)
