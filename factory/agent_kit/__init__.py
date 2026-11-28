"""The agent kit: agent-native files that carry the restricted-files and pr skills.

The same files sit at the root of this repo (for local use) and are installed
into every factory workspace before a harness runs:

  .claude/skills/restricted-files/SKILL.md   the skill (Claude Code loads it natively)
  .claude/skills/pr/SKILL.md                 the shared PR description template
  .claude/hooks/restricted_files.py          pre-tool-use hook that blocks restricted reads
  .claude/settings.json                      wires the hook and path deny rules (merged if present)
  .clinerules/hooks/PreToolUse               the same hook for Cline
  AGENTS.md, CLAUDE.md                       the rule in brief (appended if present)

Enforcement belongs to the agents' own skills and hooks; the factory only
installs the kit and checks afterwards that the agent left it untouched."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent
SENTINEL = "agent-kit:restricted-files"
COPIED = (
    ".claude/hooks/restricted_files.py",
    ".claude/skills/restricted-files/SKILL.md",
    ".claude/skills/pr/SKILL.md",
    ".clinerules/hooks/PreToolUse",
)
SETTINGS = ".claude/settings.json"
NOTES = ("AGENTS.md", "CLAUDE.md")
PATHS = (*COPIED, SETTINGS, *NOTES)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _merge_settings(existing: dict, kit: dict) -> dict:
    merged = dict(existing)
    perms = dict(merged.get("permissions") or {})
    deny = list(perms.get("deny") or [])
    deny += [rule for rule in kit["permissions"]["deny"] if rule not in deny]
    perms["deny"] = deny
    merged["permissions"] = perms
    hooks = dict(merged.get("hooks") or {})
    pre = list(hooks.get("PreToolUse") or [])
    for entry in kit["hooks"]["PreToolUse"]:
        if entry not in pre:
            pre.append(entry)
    hooks["PreToolUse"] = pre
    merged["hooks"] = hooks
    return merged


def install(dest: Path) -> dict[str, str]:
    """Install the kit into dest. Returns {path: sha256} of every kit file, for verify()."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel in COPIED:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_dir():
            shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
        shutil.copyfile(KIT_ROOT / rel, target)
        shutil.copymode(KIT_ROOT / rel, target)
    kit_settings = json.loads((KIT_ROOT / SETTINGS).read_text())
    target = dest / SETTINGS
    try:
        existing = json.loads(target.read_text()) if target.is_file() and not target.is_symlink() else {}
    except json.JSONDecodeError:
        existing = {}
    if target.is_symlink():
        target.unlink()
    target.write_text(json.dumps(_merge_settings(existing if isinstance(existing, dict) else {}, kit_settings),
                                 indent=2) + "\n")
    note = (KIT_ROOT / "AGENTS.md").read_text()
    for name in NOTES:
        target = dest / name
        if target.is_symlink():
            target.unlink()
        current = target.read_text(errors="replace") if target.is_file() else ""
        if SENTINEL not in current:
            target.write_text((current.rstrip() + "\n\n" if current.strip() else "") + note)
    return {rel: _digest(dest / rel) for rel in PATHS}


def verify(dest: Path, digests: dict[str, str]) -> list[str]:
    """Kit paths the agent changed or removed since install()."""
    return sorted(rel for rel, digest in digests.items() if _digest(dest / rel) != digest)
