"""Thin wrapper over the git executable: fixed argv lists, explicit timeouts,
no shell strings, credentials only through environment-fed helpers, and
stderr redacted before it reaches an error message.

Used by the real GitLab adapter and by the fake git host (which runs real git
against local repositories), so both exercise the same clone logic."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from factory.ports.errors import DivergedClone, PortError, SubmoduleFailure
from factory.ports.types import CheckoutResult, SubmoduleState
from factory.redaction import Redactor


class GitError(PortError):
    def __init__(self, message: str, returncode: int = -1) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass(frozen=True)
class HostAuth:
    username: str
    token: str


@dataclass
class GitOptions:
    executable: str = "git"
    timeout_seconds: float = 600.0
    clone_depth: int = 1
    submodules_enabled: bool = True
    submodules_recursive: bool = True
    submodules_depth: int = 1
    submodules_jobs: int = 4
    on_failure: str = "fail"
    url_rewrites: Sequence[tuple[str, str]] = ()
    host_auth: Mapping[str, HostAuth] = field(default_factory=dict)
    allow_protocols: str = ""
    author_name: str = "factory"
    author_email: str = "factory@localhost"


class Git:
    def __init__(self, options: GitOptions, redactor: Redactor | None = None) -> None:
        self.options = options
        self.redactor = redactor or Redactor()
        for auth in options.host_auth.values():
            self.redactor.add_secret(auth.token)

    def _global_args(self) -> list[str]:
        args = [
            "-c", f"user.name={self.options.author_name}",
            "-c", f"user.email={self.options.author_email}",
            "-c", "protocol.file.allow=always",
            "-c", "advice.detachedHead=false",
            "-c", "init.defaultBranch=main",
        ]
        for match, replace in self.options.url_rewrites:
            args += ["-c", f"url.{replace}.insteadOf={match}"]
        for i, host in enumerate(sorted(self.options.host_auth)):
            helper = f"!f() {{ echo username=$FACTORY_GIT_USER_{i}; echo password=$FACTORY_GIT_TOKEN_{i}; }}; f"
            args += ["-c", f"credential.https://{host}.helper={helper}"]
        return args

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ASKPASS": "/bin/false"})
        if self.options.allow_protocols:
            env["GIT_ALLOW_PROTOCOL"] = self.options.allow_protocols
        for i, host in enumerate(sorted(self.options.host_auth)):
            auth = self.options.host_auth[host]
            env[f"FACTORY_GIT_USER_{i}"] = auth.username
            env[f"FACTORY_GIT_TOKEN_{i}"] = auth.token
        return env

    def run(self, args: Sequence[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        argv = [self.options.executable, *self._global_args(), *args]
        try:
            proc = subprocess.run(
                argv, cwd=cwd, env=self._env(), capture_output=True, text=True,
                timeout=self.options.timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out after {self.options.timeout_seconds}s") from None
        if check and proc.returncode != 0:
            stderr = self.redactor.redact(proc.stderr.strip()).text
            raise GitError(f"git {args[0]} failed: {stderr[-800:]}", proc.returncode)
        return proc

    def out(self, args: Sequence[str], cwd: Path | None = None) -> str:
        return self.run(args, cwd).stdout.strip()

    def clone(self, url: str, dest: Path, ref: str | None = None) -> CheckoutResult:
        args = ["clone", "--no-tags"]
        if ref and _looks_like_sha(ref):
            args.append("--no-checkout")  # an arbitrary commit needs history; depth cannot be assumed
        else:
            if self.options.clone_depth > 0:
                args += ["--depth", str(self.options.clone_depth)]
            if ref:
                args += ["--branch", ref]
        args += [url, str(dest)]
        self.run(args)
        if ref and _looks_like_sha(ref):
            self.run(["checkout", "--detach", ref], cwd=dest)
        return self.update_submodules(dest)

    def pull(self, workspace: Path) -> CheckoutResult:
        branch = self.out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace)
        if branch == "HEAD":
            raise DivergedClone("workspace is on a detached HEAD; re-clone instead of pulling")
        self.run(["fetch", "--no-tags", "origin", branch], cwd=workspace)
        merged = self.run(["merge", "--ff-only", "FETCH_HEAD"], cwd=workspace, check=False)
        if merged.returncode != 0:
            raise DivergedClone(f"local branch {branch} has diverged from origin; refusing to merge or rebase")
        return self.update_submodules(workspace)

    def head(self, workspace: Path) -> str:
        return self.out(["rev-parse", "HEAD"], cwd=workspace)

    def submodule_entries(self, workspace: Path) -> list[tuple[str, str, str]]:
        """(path, url, recorded commit) for each submodule of the checked-out commit."""
        if not (workspace / ".gitmodules").exists():
            return []
        listing = self.run(["config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"], cwd=workspace, check=False)
        entries = []
        for line in listing.stdout.splitlines():
            key, _, path = line.partition(" ")
            name = key[len("submodule."):-len(".path")]
            url = self.run(["config", "-f", ".gitmodules", f"submodule.{name}.url"], cwd=workspace, check=False).stdout.strip()
            tree = self.out(["ls-tree", "HEAD", "--", path], cwd=workspace)
            parts = tree.split()
            if len(parts) >= 3 and parts[1] == "commit":
                entries.append((path, url, parts[2]))
        return entries

    def update_submodules(self, workspace: Path, prefix: str = "") -> CheckoutResult:
        states: list[SubmoduleState] = []
        if self.options.submodules_enabled:
            for path, url, recorded in self.submodule_entries(workspace):
                full = f"{prefix}{path}"
                state = self._update_one(workspace, path, full, url, recorded)
                states.append(state)
                if state.ok and self.options.submodules_recursive:
                    states.extend(self.update_submodules(workspace / path, prefix=f"{full}/").submodules)
        return CheckoutResult(workspace, self.head(workspace), tuple(states))

    def _update_one(self, workspace: Path, path: str, full: str, url: str, recorded: str) -> SubmoduleState:
        self.run(["submodule", "init", "--", path], cwd=workspace, check=False)
        base = ["submodule", "update", "--init", "--checkout"]
        attempts = []
        if self.options.submodules_depth > 0:
            attempts.append([*base, "--depth", str(self.options.submodules_depth), "--", path])
        attempts.append([*base, "--", path])
        error = ""
        for args in attempts:
            proc = self.run(args, cwd=workspace, check=False)
            if proc.returncode == 0:
                error = ""
                break
            error = self.redactor.redact(proc.stderr.strip()).text[-400:]
        checked_out = None
        if not error:
            got = self.run(["rev-parse", "HEAD"], cwd=workspace / path, check=False)
            checked_out = got.stdout.strip() if got.returncode == 0 else None
            if checked_out != recorded:
                error = f"checked out {checked_out} but superproject records {recorded}"
        state = SubmoduleState(full, _strip_userinfo(url), recorded, checked_out, not error, error)
        if error and self.options.on_failure == "fail":
            raise SubmoduleFailure(f"submodule {full} could not be checked out: {error}", full)
        return state

    def commit_all(self, workspace: Path, message: str) -> str | None:
        self.run(["add", "-A"], cwd=workspace)
        if not self.run(["status", "--porcelain"], cwd=workspace).stdout.strip():
            return None
        self.run(["commit", "-q", "-m", message], cwd=workspace)
        return self.head(workspace)


def _looks_like_sha(ref: str) -> bool:
    return len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower())


def _strip_userinfo(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        return parts._replace(netloc=netloc).geturl()
    return url


def options_from_config(config, host_auth: Mapping[str, HostAuth] | None = None, allow_protocols: str = "",
                        author_name: str = "factory", author_email: str = "factory@localhost") -> GitOptions:
    g = config.git
    return GitOptions(
        executable=g.executable,
        timeout_seconds=float(g.clone.timeout_seconds),
        clone_depth=g.clone.depth,
        submodules_enabled=g.submodules.enabled,
        submodules_recursive=g.submodules.recursive,
        submodules_depth=g.submodules.depth,
        submodules_jobs=g.submodules.jobs,
        on_failure=g.submodules.on_failure,
        url_rewrites=tuple((r.match, r.replace) for r in g.url_rewrites),
        host_auth=dict(host_auth or {}),
        allow_protocols=allow_protocols,
        author_name=author_name,
        author_email=author_email,
    )
