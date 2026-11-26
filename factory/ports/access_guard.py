from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from factory.ports.types import AccessDecision, FilteredOutput, ScanReport


@runtime_checkable
class AccessGuard(Protocol):
    """Mechanical enforcement of the access policy (base skill: no proprietary files).
    When a decision cannot be proven safe, the answer is deny."""

    def check_path(self, workspace_root: Path, path: str) -> AccessDecision:
        """Resolves symlinks and '..' before matching."""
        ...

    def check_command(self, workspace_root: Path, argv: Sequence[str]) -> AccessDecision:
        """Covers cat/grep/find and git object access (show, diff, log -p, cat-file, grep)."""
        ...

    def check_attachment(self, filename: str, content: bytes) -> AccessDecision: ...

    def scan_workspace(self, workspace_root: Path) -> ScanReport:
        """Run after every clone, pull, and submodule update. Applies strip or deny_read."""
        ...

    def filter_output(self, text: str) -> FilteredOutput: ...

    def check_diff_paths(self, paths: Sequence[str]) -> AccessDecision:
        """PR gate: no added, modified, or deleted path may match."""
        ...
