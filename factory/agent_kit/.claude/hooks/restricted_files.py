#!/usr/bin/env python3
"""Pre-tool-use hook for the restricted-files skill.

Agents call this before every file or shell tool. It blocks the call when the
tool would read, search, edit, or overwrite a file whose path or content
carries the restricted marker. It never prints file content, only paths.

Wiring:
  Claude Code  .claude/settings.json  PreToolUse -> python3 .claude/hooks/restricted_files.py
  Cline        .clinerules/hooks/PreToolUse      -> python3 .claude/hooks/restricted_files.py --format cline

Claude format: blocked calls exit 2 with the reason on stderr.
Cline format:  prints {"cancel": true, "errorMessage": "..."} on stdout.

Extra markers can be added (never removed) with RESTRICTED_MARKERS="a,b".
Text between the kit's begin and end sentinels is ignored, so the skill and
AGENTS.md, which name the marker, do not block searches of the workspace.

Standalone on purpose: stdlib only, no factory import, so it works in a
local workspace and in a factory sandbox alike."""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import sys
from pathlib import Path

# Built from parts so this file does not itself carry the marker.
BASE_MARKER = "propri" + "etary"
SENTINEL = "agent-kit:restricted-files"
KIT_REGION = re.compile(re.escape(SENTINEL + ":begin") + r".*?" + re.escape(SENTINEL + ":end"), re.S)
READ_BYTES = 1_000_000  # a file the agent reads directly
WALK_BYTES = 8192  # each file under a directory the agent searches
MAX_LISTED = 20
SKIP_DIRS = {".git"}

READERS = {"Read", "NotebookRead", "Edit", "MultiEdit", "NotebookEdit"}
SEARCHERS = {"Grep"}
LISTERS = {"Glob", "LS"}
WRITERS = {"Write"}
SHELL = {"Bash"}
RECURSIVE_TOOLS = {"rg", "ag", "ack", "find", "tree", "fd", "du"}
RECURSIVE_FLAGS = {"-r", "-R", "--recursive"}
GIT_READERS = {"grep", "show", "diff", "blame", "archive", "cat-file", "stash", "format-patch", "whatchanged"}
CLINE_TOOLS = {
    "read_file": ("Read", "path"),
    "write_to_file": ("Write", "path"),
    "replace_in_file": ("Edit", "path"),
    "search_files": ("Grep", "path"),
    "list_files": ("Glob", "path"),
    "list_code_definition_names": ("Grep", "path"),
    "execute_command": ("Bash", "command"),
}


def markers() -> tuple[str, ...]:
    extra = [m.strip().lower() for m in os.environ.get("RESTRICTED_MARKERS", "").split(",") if m.strip()]
    return (BASE_MARKER, *extra)


def marked_text(text: str) -> bool:
    text = KIT_REGION.sub("", text).lower()
    return any(m in text for m in markers())


def marked_name(path: str) -> bool:
    low = path.lower()
    return any(m in low for m in markers())


def marked_file(path: Path, limit: int) -> bool:
    try:
        with path.open("rb") as fh:
            return marked_text(fh.read(limit).decode("utf-8", errors="ignore"))
    except OSError:
        return False


def marked_under(root: Path, base: Path) -> list[str]:
    """Paths (relative to base) of restricted files at or under root."""

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(base))
        except ValueError:
            return str(p)

    if root.is_file():
        return [rel(root)] if marked_name(str(root)) or marked_file(root, READ_BYTES) else []
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            if name in SKIP_DIRS:
                dirnames.remove(name)
            elif marked_name(name):
                hits.append(rel(here / name))
                dirnames.remove(name)
        for name in filenames:
            full = here / name
            if marked_name(name) or (not full.is_symlink() and marked_file(full, WALK_BYTES)):
                hits.append(rel(full))
                if len(hits) >= MAX_LISTED:
                    return hits
    return hits


class Blocked(Exception):
    pass


def resolve(cwd: Path, value: str) -> Path:
    p = Path(os.path.expanduser(value))
    return p if p.is_absolute() else cwd / p


def check_name(value: str) -> None:
    if value and marked_name(value):
        raise Blocked(f"{value} is a restricted path")


def check_read(cwd: Path, value: str) -> None:
    check_name(value)
    target = resolve(cwd, value)
    real = Path(os.path.realpath(target))
    check_name(str(real))
    if real.is_file() and marked_file(real, READ_BYTES):
        raise Blocked(f"{value} carries the restricted marking")
    if real.is_dir():
        check_tree(cwd, real, value)


def check_tree(cwd: Path, root: Path, shown: str) -> None:
    hits = marked_under(root, cwd)
    if hits:
        listed = ", ".join(hits[:MAX_LISTED])
        raise Blocked(f"{shown} covers restricted files ({listed}). Search or list narrower paths that exclude them")


def check_tool(tool: str, params: dict, cwd: Path) -> None:
    if tool in READERS:
        check_read(cwd, str(params.get("file_path") or params.get("notebook_path") or params.get("path") or ""))
    elif tool in WRITERS:
        path = str(params.get("file_path") or params.get("path") or "")
        check_name(path)
        target = resolve(cwd, path)
        if target.is_file() and marked_file(target, READ_BYTES):
            raise Blocked(f"{path} carries the restricted marking and may not be overwritten")
    elif tool in SEARCHERS:
        pattern = str(params.get("pattern") or params.get("regex") or "")
        if marked_name(pattern):
            raise Blocked("the search pattern names the restricted marking")
        check_name(str(params.get("glob") or params.get("file_pattern") or ""))
        path = str(params.get("path") or ".")
        check_name(path)
        root = Path(os.path.realpath(resolve(cwd, path)))
        if root.exists():
            check_tree(cwd, root, path)
    elif tool in LISTERS:
        check_name(str(params.get("path") or ""))
        check_name(str(params.get("pattern") or ""))
    elif tool in SHELL:
        check_command(str(params.get("command") or ""), cwd)
    else:
        for value in params.values():
            if isinstance(value, str) and len(value) < 4096 and "\n" not in value and ("/" in value or "." in value):
                check_name(value)


def check_command(command: str, cwd: Path) -> None:
    if marked_name(command):
        raise Blocked("the command names the restricted marking")
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        tokens = command.split()
    words = [t for t in tokens if t not in ("|", "||", "&&", ";", "&")]
    exes = {os.path.basename(t) for t in words}
    recursive = bool(exes & RECURSIVE_TOOLS) or bool(set(words) & RECURSIVE_FLAGS) or any(
        re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", t) for t in words)
    if "git" in exes and (set(words) & GIT_READERS or ("log" in words and set(words) & {"-p", "--patch"})):
        recursive = True
    touched = False
    for token in words:
        if token.startswith("-") and "=" not in token:
            continue
        value = token.split("=", 1)[1] if token.startswith("-") else token
        if ":" in value and not os.path.exists(resolve(cwd, value)):
            value = value.split(":", 1)[1]  # git rev:path
        if not value:
            continue
        candidates = glob.glob(str(resolve(cwd, value))) if any(c in value for c in "*?[") else [str(resolve(cwd, value))]
        for found in candidates:
            path = Path(os.path.realpath(found))
            if not os.path.lexists(found):
                continue
            check_name(str(path))
            if path.is_file():
                touched = True
                if marked_file(path, READ_BYTES):
                    raise Blocked(f"{value} carries the restricted marking")
            elif path.is_dir() and path != Path("/"):
                if recursive:
                    touched = True
                    check_tree(cwd, path, value)
    if recursive and not touched:
        check_tree(cwd, cwd, "the working directory")


def parse(raw: dict, fmt: str) -> tuple[str, dict, Path]:
    if fmt == "cline":
        call = raw.get("preToolUse") or {}
        name = str(call.get("toolName") or call.get("tool") or "")
        params = dict(call.get("parameters") or {})
        roots = raw.get("workspaceRoots") or []
        cwd = Path(roots[0]) if roots else Path.cwd()
        tool, key = CLINE_TOOLS.get(name, (name, "path"))
        if tool == "Bash":
            params = {"command": params.get(key, "")}
        elif key in params:
            params = {**params, "file_path": params[key], "path": params[key]}
        return tool, params, cwd
    return str(raw.get("tool_name") or ""), dict(raw.get("tool_input") or {}), Path(raw.get("cwd") or os.getcwd())


def main(argv: list[str]) -> int:
    fmt = "cline" if "--format" in argv and argv[argv.index("--format") + 1:][:1] == ["cline"] else "claude"
    try:
        raw = json.loads(sys.stdin.read() or "{}")
        tool, params, cwd = parse(raw, fmt)
        check_tool(tool, params, cwd)
    except Blocked as exc:
        reason = (f"Blocked by the restricted-files skill: {exc}. Do not try another way to read it. "
                  "If the task needs it, stop and report blocked, naming the path only.")
        if fmt == "cline":
            print(json.dumps({"cancel": True, "errorMessage": reason, "contextModification": reason}))
            return 0
        print(reason, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a broken hook must fail closed, not open
        reason = f"restricted-files hook error ({type(exc).__name__}); the call was blocked to be safe"
        if fmt == "cline":
            print(json.dumps({"cancel": True, "errorMessage": reason}))
            return 0
        print(reason, file=sys.stderr)
        return 2
    if fmt == "cline":
        print(json.dumps({"cancel": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
