"""Stage skills: optional third-party skills selected per stage in config and
delivered to the model in two ways:

- inline: every selected skill (SKILL.md body and its markdown references) is
  rendered into the stage's instructions after the mandatory base skills and
  the factory adapter preamble;
- native: for CLI agents that load skills from their home directory (Claude
  Code: ~/.claude/skills, Codex: ~/.codex/skills), the same skill folders are
  installed into the sandbox home, outside the repository copy, so a skill's
  relative references resolve.

Vendored skills are never edited; the adapter preamble (factory/skills/adapter.md)
carries the factory's adaptations."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from factory.config.schema import FactoryConfig, SkillRef
from factory.ports.types import Ticket

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
VENDOR = SKILLS_ROOT / "vendor"
ADAPTER = SKILLS_ROOT / "adapter.md"
FRONT = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
STAGES = ("spec", "implement", "review", "improve")


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    root: Path

    def body(self) -> str:
        return FRONT.sub("", (self.root / "SKILL.md").read_text(errors="replace")).strip()

    def references(self) -> list[tuple[str, str]]:
        out = []
        for path in sorted(self.root.rglob("*.md")):
            if path.name != "SKILL.md" and path.name != "CREDITS.md":
                out.append((str(path.relative_to(self.root)), path.read_text(errors="replace").strip()))
        return out


def _front(text: str) -> dict[str, str]:
    m = FRONT.match(text)
    meta = {}
    for line in (m.group(1).splitlines() if m else []):
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("'\"")
    return meta


def scan(roots: Iterable[Path]) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/*/SKILL.md")):
            folder = skill_md.parent
            meta = _front(skill_md.read_text(errors="replace"))
            sid = f"{folder.parent.name}/{folder.name}"
            found[sid] = Skill(sid, meta.get("name", folder.name), meta.get("description", ""), folder)
    return found


@cache
def _vendored() -> tuple[tuple[str, Skill], ...]:
    return tuple(scan([VENDOR]).items())


def catalog(cfg: FactoryConfig, base_dir: Path | None = None) -> dict[str, Skill]:
    skills = dict(_vendored())
    extra = [(base_dir or Path.cwd()) / p for p in cfg.skills.extra_paths]
    skills.update(scan(extra))
    return skills


def unknown(cfg: FactoryConfig, base_dir: Path | None = None) -> list[str]:
    known = catalog(cfg, base_dir)
    return sorted({ref.skill for stage in STAGES for ref in getattr(cfg.skills.stages, stage)
                   if ref.skill not in known})


def select(cfg: FactoryConfig, stage: str, ticket: Ticket | None, has_spec: bool,
           base_dir: Path | None = None) -> list[Skill]:
    if not cfg.skills.enabled:
        return []
    known = catalog(cfg, base_dir)
    chosen = []
    for ref in getattr(cfg.skills.stages, stage):
        if _applies(ref, ticket, has_spec) and ref.skill in known and known[ref.skill] not in chosen:
            chosen.append(known[ref.skill])
    return chosen


def _applies(ref: SkillRef, ticket: Ticket | None, has_spec: bool) -> bool:
    if ref.requires_spec and not has_spec:
        return False
    return not (ref.issue_types and (ticket is None or ticket.issue_type not in ref.issue_types))


def render(skills: Sequence[Skill], max_chars: int, artifacts_dir: str = "", installed_at: Sequence[str] = ()) -> str:
    if not skills:
        return ""
    adapter = ADAPTER.read_text().strip()
    adapter = adapter.replace("`$FACTORY_ARTIFACTS` if that directory is given",
                              f"`{artifacts_dir}`" if artifacts_dir else "`$FACTORY_ARTIFACTS` if that directory is given")
    parts = [adapter, "## Skills for this stage"]
    if installed_at:
        parts.append("They are also installed for your CLI under " + ", ".join(f"`~/{d}`" for d in installed_at)
                     + " so their relative references resolve.")
    used = sum(len(p) for p in parts)
    dropped = []
    for skill in skills:
        block = [f"### Skill `{skill.id}`", f"_{skill.description}_" if skill.description else "", skill.body()]
        block += [f"#### `{skill.id}` reference: {rel}\n\n{text}" for rel, text in skill.references()]
        text = "\n\n".join(b for b in block if b)
        if used + len(text) > max_chars:
            dropped.append(skill.id)
            continue
        parts.append(text)
        used += len(text)
    if dropped:
        parts.append("Skills left out to stay within skills.max_chars_per_stage: " + ", ".join(dropped))
    return "\n\n".join(parts)


def install(skills: Sequence[Skill], home: Path, dirs: Sequence[str]) -> list[str]:
    installed = []
    for rel in dirs:
        target_root = home / rel
        for skill in skills:
            target = target_root / skill.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(skill.root, target, ignore=shutil.ignore_patterns("scripts", "*.sh"))
        installed.append(rel)
    return installed
