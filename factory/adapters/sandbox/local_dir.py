"""Plain-directory sandbox rooted at a configured path. Each run gets a fresh
directory; commands run with a scrubbed environment, resource limits, their
own process group (killed whole on timeout), and argv only (no shell).

It does not isolate the network or the filesystem view; HIDES_PATHS is False.
A bubblewrap or container sandbox can replace it by config alone."""

from __future__ import annotations

import fnmatch
import mimetypes
import os
import resource
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from factory.ports.context import AdapterContext
from factory.ports.errors import InvalidRequest
from factory.ports.types import Artifact, Command, CommandResult, Workspace, WorkspaceSpec

PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ")


class LocalDirSandbox:
    HIDES_PATHS = False

    def __init__(self, root: Path, extra_env: Mapping[str, str] | None = None,
                 cpu_seconds: int = 1800, memory_mb: int = 0, max_output_bytes: int = 2_000_000) -> None:
        self.root = root
        self.extra_env = dict(extra_env or {})
        self.cpu_seconds = cpu_seconds
        self.memory_mb = memory_mb
        self.max_output_bytes = max_output_bytes

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> LocalDirSandbox:
        return cls(ctx.base_dir / str(settings.get("root", "var/workspaces")),
                   {str(k): str(v) for k, v in dict(settings.get("env", {})).items()},
                   int(settings.get("cpu_seconds", 1800)), int(settings.get("memory_mb", 0)))

    def prepare(self, spec: WorkspaceSpec) -> Workspace:
        path = self.root / spec.run_id
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return Workspace(spec.run_id, path)

    def _inside(self, workspace: Workspace, rel: str) -> Path:
        target = (workspace.root / rel).resolve()
        if target != workspace.root.resolve() and workspace.root.resolve() not in target.parents:
            raise InvalidRequest(f"path {rel!r} escapes the workspace")
        return target

    def _limits(self) -> None:
        if self.cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (self.cpu_seconds, self.cpu_seconds))
        if self.memory_mb:
            limit = self.memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    def run(self, workspace: Workspace, command: Command) -> CommandResult:
        if not command.argv or not all(isinstance(a, str) for a in command.argv):
            raise InvalidRequest("command must be a non-empty argv list")
        cwd = self._inside(workspace, command.cwd)
        env = {k: os.environ[k] for k in PASSTHROUGH if k in os.environ}
        env.update({"HOME": str(workspace.root), "TMPDIR": str(workspace.root / ".tmp")})
        env.update(self.extra_env)
        env.update(command.env)
        (workspace.root / ".tmp").mkdir(exist_ok=True)
        start = time.monotonic()
        stdin = open(command.stdin_path, "rb") if command.stdin_path else subprocess.DEVNULL  # noqa: SIM115
        try:
            proc = subprocess.Popen(list(command.argv), cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, preexec_fn=self._limits)
        except FileNotFoundError:
            return CommandResult(127, "", f"{command.argv[0]}: command not found", 0.0, False)
        finally:
            if command.stdin_path:
                stdin.close()
        timed_out = False
        try:
            out, err = proc.communicate(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
        cap = self.max_output_bytes
        return CommandResult(proc.returncode if not timed_out else -9,
                             out[-cap:].decode(errors="replace"), err[-cap:].decode(errors="replace"),
                             time.monotonic() - start, timed_out)

    def collect_artifacts(self, workspace: Workspace, patterns: Sequence[str]) -> tuple[Artifact, ...]:
        found = []
        for path in sorted(workspace.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = str(path.relative_to(workspace.root))
            if any(fnmatch.fnmatch(rel, p) for p in patterns):
                found.append(Artifact(rel, path, mimetypes.guess_type(rel)[0] or "application/octet-stream"))
        return tuple(found)

    def teardown(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)
