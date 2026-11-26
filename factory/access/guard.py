"""Mechanical enforcement of the mandatory no-proprietary-files base skill.

Harness code, never the model, calls this. Decisions are deny-by-default
whenever safety cannot be shown. Results and audit records carry paths only,
never content."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from factory.metrics import Metrics
from factory.ports.context import AdapterContext
from factory.ports.types import AccessDecision, FilteredOutput, ScanReport
from factory.redaction import POLICY_PLACEHOLDER

FILE_TOOLS = frozenset({
    "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "find", "ls", "tree", "wc", "sed",
    "awk", "cut", "sort", "uniq", "diff", "cmp", "xxd", "od", "hexdump", "strings", "file", "stat", "nl", "tac",
    "base64", "md5sum", "sha256sum", "cp", "mv", "tar", "zip", "unzip", "gzip", "zcat", "jq", "yq",
})
INLINE_CODE = {"sh": "-c", "bash": "-c", "zsh": "-c", "dash": "-c", "ksh": "-c", "fish": "-c",
               "python": "-c", "python3": "-c", "perl": "-e", "ruby": "-e", "node": "-e"}
ALWAYS_DENY = frozenset({"eval", "exec", "sudo", "su", "doas", "xargs", "env", "nohup", "script", "ssh", "scp"})
GIT_OBJECT_CMDS = frozenset({"show", "diff", "log", "cat-file", "grep", "blame", "archive", "format-patch",
                             "rev-list", "whatchanged", "annotate", "difftool", "stash", "bundle", "fast-export"})
GIT_ALWAYS_UNPROVABLE = frozenset({"cat-file", "archive", "fast-export", "bundle"})


@dataclass(frozen=True)
class Policy:
    enabled: bool = True
    patterns: tuple[str, ...] = ("proprietary",)
    match_on: frozenset[str] = frozenset({"path", "content"})
    case_insensitive: bool = True
    content_scan_bytes: int = 8192
    mode: str = "strip"


@dataclass
class Denial:
    kind: str
    target: str
    reason: str


@dataclass
class PolicyAccessGuard:
    policy: Policy
    metrics: Metrics = field(default_factory=Metrics)
    trusted_commands: tuple[tuple[str, ...], ...] = ()
    denials: list[Denial] = field(default_factory=list)
    _hidden: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> PolicyAccessGuard:
        ap = ctx.config.access_policy
        policy = Policy(ap.enabled, tuple(ap.deny_patterns), frozenset(ap.match_on), ap.case_insensitive,
                        ap.content_scan_bytes, ap.mode)
        return cls(policy, ctx.metrics)

    def derive(self, extra_patterns: Iterable[str] = (), trusted_commands: Iterable[Sequence[str]] = ()) -> PolicyAccessGuard:
        """A per-run guard. Patterns can only be added, never removed."""
        patterns = list(self.policy.patterns)
        for p in extra_patterns:
            if p and p not in patterns:
                patterns.append(p)
        policy = Policy(self.policy.enabled, tuple(patterns), self.policy.match_on, self.policy.case_insensitive,
                        self.policy.content_scan_bytes, self.policy.mode)
        return PolicyAccessGuard(policy, self.metrics, tuple(tuple(c) for c in trusted_commands))

    # ---- matching -------------------------------------------------------
    def _norm(self, text: str) -> str:
        return text.lower() if self.policy.case_insensitive else text

    def text_matches(self, text: str) -> str | None:
        hay = self._norm(text)
        for pattern in self.policy.patterns:
            if self._norm(pattern) in hay:
                return pattern
        return None

    def _path_matches(self, rel: str) -> str | None:
        if "path" not in self.policy.match_on:
            return None
        return self.text_matches(rel)

    def _content_matches(self, path: Path) -> str | None:
        if "content" not in self.policy.match_on:
            return None
        try:
            with path.open("rb") as fh:
                head = fh.read(self.policy.content_scan_bytes)
        except OSError:
            return None
        return self.text_matches(head.decode("utf-8", errors="ignore"))

    def content_matches(self, data: bytes) -> str | None:
        if "content" not in self.policy.match_on:
            return None
        return self.text_matches(data[: self.policy.content_scan_bytes].decode("utf-8", errors="ignore"))

    def _deny(self, kind: str, target: str, reason: str, pattern: str | None = None) -> AccessDecision:
        self.denials.append(Denial(kind, target, reason))
        self.metrics.incr("access_denied", kind=kind)
        return AccessDecision(False, reason, pattern)

    # ---- port -----------------------------------------------------------
    def check_path(self, workspace_root: Path, path: str) -> AccessDecision:
        if not self.policy.enabled:
            return AccessDecision(True)
        root = Path(os.path.realpath(workspace_root))
        pattern = self._path_matches(path)
        if pattern:
            return self._deny("path", path, "path matches the access policy", pattern)
        candidate = Path(path) if os.path.isabs(path) else workspace_root / path
        resolved = Path(os.path.realpath(candidate))
        if resolved != root and root not in resolved.parents:
            return self._deny("path", path, "path resolves outside the workspace")
        rel = str(resolved.relative_to(root)) if resolved != root else "."
        if ".git" in PurePosixPath(rel).parts:
            return self._deny("path", path, "git internals are not readable through tools")
        pattern = self._path_matches(rel)
        if pattern:
            return self._deny("path", path, "path resolves to a file matching the access policy", pattern)
        if rel in self._hidden.get(str(root), set()):
            return self._deny("path", path, "file is hidden by the access policy")
        if resolved.is_file():
            pattern = self._content_matches(resolved)
            if pattern:
                return self._deny("content", path, "file content carries a restricted marking", pattern)
        if resolved.is_dir() and self.policy.mode == "deny_read" and self._dir_has_hidden(root, rel):
            return AccessDecision(True, "directory listing will be filtered")
        return AccessDecision(True)

    def _dir_has_hidden(self, root: Path, rel: str) -> bool:
        prefix = "" if rel == "." else rel.rstrip("/") + "/"
        return any(h.startswith(prefix) for h in self._hidden.get(str(root), set()))

    def check_command(self, workspace_root: Path, argv: Sequence[str]) -> AccessDecision:
        if not self.policy.enabled:
            return AccessDecision(True)
        if not argv:
            return self._deny("command", "", "empty command")
        if tuple(argv) in self.trusted_commands:
            return AccessDecision(True)
        exe = os.path.basename(argv[0])
        shown = " ".join(argv)[:200]
        for arg in argv:
            pattern = self.text_matches(arg)
            if pattern:
                return self._deny("command", exe, "command references a path matching the access policy", pattern)
        if exe in ALWAYS_DENY:
            return self._deny("command", exe, f"{exe} cannot be proven safe")
        flag = INLINE_CODE.get(exe)
        if flag and flag in argv[1:]:
            return self._deny("command", exe, "inline code cannot be proven safe")
        for arg in argv[1:]:
            value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            if value.startswith("-"):
                continue
            if exe == "git" and ":" in value:
                value = value.split(":", 1)[1] or value
            target = workspace_root / value
            if os.path.lexists(target):
                decision = self.check_path(workspace_root, value)
                if not decision.allowed:
                    return decision
        if exe == "git":
            return self._check_git(workspace_root, list(argv[1:]), shown)
        if self.policy.mode == "deny_read":
            if exe not in FILE_TOOLS:
                return self._deny("command", exe, "in deny_read mode only known file tools and declared repo commands run")
            hidden = self._hidden.get(str(Path(os.path.realpath(workspace_root))))
            recursive = exe in ("find", "tree", "rg", "ag") or any(a in ("-r", "-R", "--recursive") for a in argv[1:])
            if hidden and recursive:
                return self._deny("command", exe, "recursive reads cannot skip hidden files")
        return AccessDecision(True)

    def _check_git(self, root: Path, args: list[str], shown: str) -> AccessDecision:
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub not in GIT_OBJECT_CMDS:
            return AccessDecision(True)
        if self.policy.mode == "strip":
            return AccessDecision(True)
        if sub in GIT_ALWAYS_UNPROVABLE:
            return self._deny("command", "git", f"git {sub} cannot be proven safe in deny_read mode")
        if "--" not in args:
            return self._deny("command", "git", f"git {sub} without an explicit pathspec cannot be proven safe")
        for spec in args[args.index("--") + 1:]:
            decision = self.check_path(root, spec)
            if not decision.allowed:
                return decision
            if (root / spec).is_dir() and self._dir_has_hidden(Path(os.path.realpath(root)), spec):
                return self._deny("command", "git", "pathspec covers hidden files")
        return AccessDecision(True)

    def check_attachment(self, filename: str, content: bytes) -> AccessDecision:
        if not self.policy.enabled:
            return AccessDecision(True)
        pattern = self._path_matches(filename)
        if pattern:
            return self._deny("attachment", filename, "attachment name matches the access policy", pattern)
        pattern = self.content_matches(content)
        if pattern:
            return self._deny("attachment", filename, "attachment content carries a restricted marking", pattern)
        return AccessDecision(True)

    def scan_workspace(self, workspace_root: Path) -> ScanReport:
        if not self.policy.enabled:
            return ScanReport((), "disabled")
        root = Path(os.path.realpath(workspace_root))
        matched: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            here = Path(dirpath)
            for name in list(dirnames):
                rel = str((here / name).relative_to(root))
                if (here / name).is_symlink() or self._path_matches(rel):
                    if (here / name).is_symlink() and not self._symlink_unsafe(root, here / name, rel):
                        continue
                    matched.append(rel)
                    dirnames.remove(name)
            for name in filenames:
                if name == ".git":
                    continue
                full = here / name
                rel = str(full.relative_to(root))
                if full.is_symlink():
                    if self._symlink_unsafe(root, full, rel):
                        matched.append(rel)
                    continue
                if self._path_matches(rel) or self._content_matches(full):
                    matched.append(rel)
        if self.policy.mode == "strip":
            for rel in matched:
                target = root / rel
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
        else:
            self._hidden[str(root)] = set(matched)
        self.metrics.incr("access_scan_matches", len(matched))
        return ScanReport(tuple(sorted(matched)), self.policy.mode)

    def _symlink_unsafe(self, root: Path, link: Path, rel: str) -> bool:
        target = Path(os.path.realpath(link))
        if target != root and root not in target.parents:
            return True
        if self._path_matches(rel) or self._path_matches(str(target.relative_to(root))):
            return True
        return target.is_file() and self._content_matches(target) is not None

    def filter_output(self, text: str) -> FilteredOutput:
        if not self.policy.enabled or not text:
            return FilteredOutput(text, 0)
        lines = text.split("\n")
        count = 0
        for i, line in enumerate(lines):
            if self.text_matches(line):
                lines[i] = POLICY_PLACEHOLDER
                count += 1
        if count:
            self.metrics.incr("access_output_redactions", count)
        return FilteredOutput("\n".join(lines), count)

    def check_diff_paths(self, paths: Sequence[str]) -> AccessDecision:
        if not self.policy.enabled:
            return AccessDecision(True)
        for path in paths:
            pattern = self._path_matches(path)
            if pattern:
                return self._deny("diff", path, "change touches a path matching the access policy", pattern)
        return AccessDecision(True)

    def is_visible(self, workspace_root: Path, rel: str) -> bool:
        """For filtering listings and search results; never audited."""
        if not self.policy.enabled:
            return ".git" not in PurePosixPath(rel).parts
        root = str(Path(os.path.realpath(workspace_root)))
        return (".git" not in PurePosixPath(rel).parts and not self._path_matches(rel)
                and rel not in self._hidden.get(root, set()))

    def hide(self, workspace_root: Path, paths: Iterable[str]) -> None:
        self._hidden[str(Path(os.path.realpath(workspace_root)))] = set(paths)
