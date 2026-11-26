"""Isolated workspaces for harnesses.

The harness gets a copy of the checked-out tree (submodules flattened in as
plain files, access policy already applied) with git history replaced by one
baseline commit. The factory diffs the copy against that baseline and carries
accepted changes into the real clone itself."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from factory.vcs.git import Git, GitOptions


@dataclass(frozen=True)
class Change:
    path: str
    status: str  # A, M, D, T (type change)
    is_symlink: bool = False


@dataclass(frozen=True)
class LineStats:
    added: int
    deleted: int
    added_lines: tuple[str, ...]


def local_git() -> Git:
    return Git(GitOptions(clone_depth=0, submodules_enabled=False, author_name="factory-baseline",
                          author_email="baseline@factory.invalid", allow_protocols="file"))


def isolate(clone_root: Path, dest: Path) -> str:
    def ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n == ".git"]

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(clone_root, dest, symlinks=True, ignore=ignore)
    git = local_git()
    git.run(["init", "-q", "-b", "baseline"], cwd=dest)
    git.run(["add", "-A", "--force"], cwd=dest)
    git.run(["commit", "-q", "--allow-empty", "-m", "baseline"], cwd=dest)
    return git.head(dest)


def changes(copy_root: Path) -> list[Change]:
    git = local_git()
    git.run(["add", "-A", "--force"], cwd=copy_root)
    out = git.run(["diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD"], cwd=copy_root).stdout
    parts = [p for p in out.split("\0") if p]
    found = []
    for status, path in zip(parts[::2], parts[1::2]):
        found.append(Change(path, status[0], (copy_root / path).is_symlink()))
    return found


def line_stats(copy_root: Path) -> LineStats:
    git = local_git()
    numstat = git.run(["diff", "--cached", "--numstat", "HEAD"], cwd=copy_root).stdout
    added = deleted = 0
    for line in numstat.splitlines():
        a, d, _ = line.split("\t", 2)
        added += int(a) if a.isdigit() else 0
        deleted += int(d) if d.isdigit() else 0
    patch = git.run(["diff", "--cached", "-U0", "HEAD"], cwd=copy_root).stdout
    lines = tuple(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    return LineStats(added, deleted, lines)


def diff_text(copy_root: Path) -> str:
    return local_git().run(["diff", "--cached", "HEAD"], cwd=copy_root).stdout


def apply(found: list[Change], copy_root: Path, clone_root: Path) -> list[str]:
    paths = []
    for change in found:
        target = clone_root / change.path
        if change.status == "D":
            if target.is_symlink() or target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
            source = copy_root / change.path
            if source.is_symlink():
                os.symlink(os.readlink(source), target)
            else:
                shutil.copy2(source, target)
        paths.append(change.path)
    return paths
