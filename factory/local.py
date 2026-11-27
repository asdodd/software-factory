"""Local workspaces: use the factory's skills and tools with your own agent,
without the factory pipeline.

A workspace is a directory with the agent kit at its root (the
restricted-files skill, AGENTS.md and CLAUDE.md, and hooks for Claude Code and
Cline), a workspace.toml listing repos, and a repos/ directory they are cloned
into. This repo is a workspace as cloned; `factory workspace init DIR` makes
another one anywhere.

  factory workspace init [DIR]                    kit + workspace.toml + repos/
  factory workspace add URL [--name N] [--branch B]
  factory workspace sync [NAME...]                clone missing repos, fast-forward the rest
  factory workspace list
  factory workspace ticket KEY                    optional: pull a ticket's text and repos in

Start your agent at the workspace root so the kit applies to every repo. Each
cloned repo also gets the kit's files where it has none of its own, listed in
the repo's .git/info/exclude so they never show up in git status.

Git runs as you, with your own credentials. Nothing here talks to intake."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from factory import agent_kit
from factory.core.readiness import text_field, ticket_repos
from factory.core.workspace import repo_dirs

CONFIG = "workspace.toml"
TEMPLATE = """\
# Local workspace. Repos listed here are cloned into repos_dir by
# `factory workspace sync`; `factory workspace add URL` appends to this file.
# Start your agent (Claude Code, Codex, Cline, ...) at this directory so the
# restricted-files skill and its hooks apply to every repo.

[workspace]
repos_dir = "repos"
tickets_dir = "tickets"
# For `factory workspace ticket KEY`: how a ticket's repo path becomes a clone URL.
# git_url_template = "git@gitlab.example.com:{path}.git"
git_url_template = ""

# [[repos]]
# name = "portal"                                  # directory under repos_dir
# url = "git@gitlab.example.com:web/portal.git"
# branch = "main"                                  # optional; default branch otherwise
"""
IGNORED = ("repos/", "tickets/", "workspace.toml")


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepoEntry:
    name: str
    url: str
    branch: str = ""


@dataclass
class Workspace:
    root: Path
    repos_dir: str = "repos"
    tickets_dir: str = "tickets"
    git_url_template: str = ""
    repos: list[RepoEntry] = field(default_factory=list)

    def repo_path(self, entry: RepoEntry) -> Path:
        return self.root / self.repos_dir / entry.name


def find_root(start: Path) -> Path:
    """The nearest directory at or above start with a workspace.toml or the kit; start otherwise."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / CONFIG).is_file() or (d / agent_kit.COPIED[0]).is_file():
            return d
    return start


def load(root: Path) -> Workspace:
    path = root / CONFIG
    data = tomllib.loads(path.read_text()) if path.is_file() else {}
    ws = data.get("workspace", {})
    repos = [RepoEntry(str(r["name"]), str(r["url"]), str(r.get("branch", ""))) for r in data.get("repos", [])]
    return Workspace(root, str(ws.get("repos_dir", "repos")), str(ws.get("tickets_dir", "tickets")),
                     str(ws.get("git_url_template", "")), repos)


def init(root: Path) -> list[str]:
    """Make root a workspace. Safe to run again: it never overwrites workspace.toml."""
    done = []
    root.mkdir(parents=True, exist_ok=True)
    agent_kit.install(root)
    done.append("agent kit installed (.claude/, .clinerules/, AGENTS.md, CLAUDE.md)")
    if not (root / CONFIG).is_file():
        (root / CONFIG).write_text(TEMPLATE)
        done.append(f"wrote {CONFIG}")
    ws = load(root)
    (root / ws.repos_dir).mkdir(exist_ok=True)
    gitignore = root / ".gitignore"
    current = gitignore.read_text() if gitignore.is_file() else ""
    missing = [line for line in (f"{ws.repos_dir}/", f"{ws.tickets_dir}/", CONFIG) if line not in current.splitlines()]
    if missing:
        gitignore.write_text(current + ("" if not current or current.endswith("\n") else "\n") + "\n".join(missing) + "\n")
        done.append(f".gitignore: {', '.join(missing)}")
    return done


def _name_from_url(url: str) -> str:
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _toml_str(value: str) -> str:
    return json.dumps(value)


def add(root: Path, url: str, name: str = "", branch: str = "") -> RepoEntry:
    ws = load(root)
    if not (root / CONFIG).is_file():
        init(root)
    entry = RepoEntry(name or _name_from_url(url), url, branch)
    if not re.fullmatch(r"[\w.-]+", entry.name) or entry.name in (".", ".."):
        raise WorkspaceError(f"invalid repo name {entry.name!r}; pass --name")
    for existing in ws.repos:
        if existing.name == entry.name:
            if existing.url != url:
                raise WorkspaceError(f"a repo named {entry.name!r} is already listed with {existing.url}")
            return existing
    block = f"\n[[repos]]\nname = {_toml_str(entry.name)}\nurl = {_toml_str(url)}\n"
    if branch:
        block += f"branch = {_toml_str(branch)}\n"
    with (root / CONFIG).open("a") as fh:
        fh.write(block)
    return entry


def _git(args: Sequence[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def install_repo_kit(repo: Path) -> list[str]:
    """Copy kit files the repo does not have, and keep them out of git status."""
    added = []
    for rel in (*agent_kit.COPIED, agent_kit.SETTINGS, "AGENTS.md", "CLAUDE.md"):
        target = repo / rel
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((agent_kit.KIT_ROOT / ("AGENTS.md" if rel == "CLAUDE.md" else rel)).read_bytes())
        target.chmod((agent_kit.KIT_ROOT / ("AGENTS.md" if rel == "CLAUDE.md" else rel)).stat().st_mode)
        added.append(rel)
    exclude = repo / ".git" / "info" / "exclude"
    if added and exclude.parent.is_dir():
        current = exclude.read_text() if exclude.is_file() else ""
        lines = [f"/{rel}" for rel in added if f"/{rel}" not in current.splitlines()]
        if lines:
            exclude.write_text(current + ("" if not current or current.endswith("\n") else "\n")
                               + "# agent kit (factory workspace)\n" + "\n".join(lines) + "\n")
    return added


def sync(root: Path, names: Sequence[str] = ()) -> list[str]:
    ws = load(root)
    wanted = [r for r in ws.repos if not names or r.name in names]
    unknown = sorted(set(names) - {r.name for r in ws.repos})
    if unknown:
        raise WorkspaceError(f"not in {CONFIG}: {', '.join(unknown)}")
    out = []
    for entry in wanted:
        path = ws.repo_path(entry)
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            args = ["clone", "--recurse-submodules", *(["--branch", entry.branch] if entry.branch else []),
                    entry.url, str(path)]
            _git(args)
            out.append(f"{entry.name}: cloned")
        else:
            _git(["pull", "--ff-only", "--recurse-submodules"], cwd=path)
            out.append(f"{entry.name}: up to date")
        added = install_repo_kit(path)
        if added:
            out.append(f"{entry.name}: kit files added ({', '.join(added)})")
        skipped = [rel for rel in (agent_kit.SETTINGS,) if rel not in added and not _has_kit(path / rel)]
        if skipped:
            out.append(f"{entry.name}: has its own {skipped[0]}, so the hook applies only when the agent starts "
                       "at the workspace root")
    return out


def _has_kit(path: Path) -> bool:
    return path.is_file() and "restricted_files.py" in path.read_text(errors="replace")


def status(root: Path) -> list[str]:
    ws = load(root)
    lines = [f"workspace: {root}"]
    for entry in ws.repos:
        path = ws.repo_path(entry)
        state = "cloned" if (path / ".git").exists() else "not cloned (run sync)"
        lines.append(f"  {entry.name:<24} {state:<22} {entry.url}{' @ ' + entry.branch if entry.branch else ''}")
    if not ws.repos:
        lines.append("  (no repos; add one with `factory workspace add URL`)")
    return lines


def ticket(root: Path, tickets, key: str, clone: bool = True) -> list[str]:
    """Write a ticket's text, comments, and attachments into tickets/KEY*, and
    add and clone the repos it names (needs git_url_template)."""
    ws = load(root)
    t = tickets.get(key)
    folder = root / ws.tickets_dir / key
    folder.mkdir(parents=True, exist_ok=True)
    comments, cursor = [], None
    while True:
        page = tickets.list_comments(key, cursor)
        comments.extend(page.items)
        if not page.next_cursor or not page.items:
            break
        cursor = page.next_cursor
    repos = ticket_repos(t)
    parts = [f"# {t.key}: {t.summary}", f"Status: {t.status}. Type: {t.issue_type}. Priority: {t.priority}.",
             f"Repos: {', '.join(repos) or '(none named)'}", "## Description", t.description.strip() or "(none)",
             "## Acceptance criteria", text_field(t, "acceptance_criteria") or "(none stated)"]
    if text_field(t, "verify_steps"):
        parts += ["## How to verify", text_field(t, "verify_steps")]
    if comments:
        parts += ["## Comments", *(f"**{c.author.display_name or c.author.id}** ({c.created:%Y-%m-%d}):\n{c.body}"
                                   for c in comments)]
    for att in t.attachments:
        (folder / "attachments").mkdir(exist_ok=True)
        (folder / "attachments" / Path(att.filename).name).write_bytes(tickets.get_attachment(key, att.id))
    if t.attachments:
        parts += ["## Attachments", *(f"- attachments/{Path(a.filename).name}" for a in t.attachments)]
    (folder / "TICKET.md").write_text("\n\n".join(parts) + "\n")
    out = [f"wrote {(folder / 'TICKET.md').relative_to(root)}"]
    if repos and not ws.git_url_template:
        out.append("set workspace.git_url_template in workspace.toml to clone the ticket's repos")
        return out
    names = []
    dirs = repo_dirs(repos)
    for path in repos:
        names.append(add(root, ws.git_url_template.format(path=path), name=dirs[path]).name)
    if clone and names:
        out += sync(root, names)
    return out
