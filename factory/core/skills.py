"""Factory rules and stage skills, rendered to the deployment's capabilities.

Every text an agent receives (base skills, the adapter preamble, vendored and
custom skills) first goes through factory/core/conditional.py, so passages
that need sub-agents or internet access appear only when [capabilities]
allows them. A skill folder may carry a factory.toml sidecar:

    requires = ["internet"]                 # the whole skill
    [files."HTML-REPORT.md"]
    requires = ["internet"]                 # one reference file

Skills in factory/skills/vendor-online are loaded only when internet is allowed.

Delivery to CLI agents ([skills].delivery):
- native: the rules go to the agent's global memory file in the sandbox home
  (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, Cline's rules folder) and stage
  skills to its skills folder (~/.claude/skills, ~/.codex/skills), rendered;
  the task prompt names the skills to load. The mandatory base skills are also
  kept in the prompt, since tokens are cheap and they must not be missed.
- inline: everything goes in the task prompt, which saves the skill-loading turns.
Agents without a native mechanism always get the inline form."""

from __future__ import annotations

import re
import shutil
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from factory.config.schema import FactoryConfig, SkillRef
from factory.core import conditional
from factory.ports.types import Ticket

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
VENDOR = SKILLS_ROOT / "vendor"
VENDOR_ONLINE = SKILLS_ROOT / "vendor-online"
ADAPTER = SKILLS_ROOT / "adapter.md"
BASE = SKILLS_ROOT / "base"
FRONT = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
NOTICE = re.compile(r"<!-- Modified by software-factory:.*?-->\n*", re.S)
STAGES = ("spec", "implement", "review", "improve")


def capabilities(cfg: FactoryConfig) -> dict[str, bool]:
    return {"subagents": cfg.capabilities.subagents, "internet": cfg.capabilities.internet}


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    root: Path
    requires: tuple[str, ...] = ()
    file_requires: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def available(self, caps: Mapping[str, bool]) -> bool:
        return all(caps.get(r, False) for r in self.requires)

    def _keep(self, rel: str, caps: Mapping[str, bool]) -> bool:
        return all(caps.get(r, False) for r in self.file_requires.get(rel, ()))

    def body(self, caps: Mapping[str, bool]) -> str:
        raw = NOTICE.sub("", FRONT.sub("", (self.root / "SKILL.md").read_text(errors="replace")))
        return conditional.render(raw, caps, f"{self.id}/SKILL.md").strip()

    def references(self, caps: Mapping[str, bool]) -> list[tuple[str, str]]:
        out = []
        for path in sorted(self.root.rglob("*.md")):
            rel = str(path.relative_to(self.root))
            if path.name in ("SKILL.md", "CREDITS.md") or not self._keep(rel, caps):
                continue
            raw = NOTICE.sub("", path.read_text(errors="replace"))
            out.append((rel, conditional.render(raw, caps, f"{self.id}/{rel}").strip()))
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
            sidecar = {}
            if (folder / "factory.toml").is_file():
                sidecar = tomllib.loads((folder / "factory.toml").read_text())
            sid = f"{folder.parent.name}/{folder.name}"
            found[sid] = Skill(sid, meta.get("name", folder.name), meta.get("description", ""), folder,
                               tuple(sidecar.get("requires", ())),
                               {k: tuple(v.get("requires", ())) for k, v in sidecar.get("files", {}).items()})
    return found


def catalog(cfg: FactoryConfig, base_dir: Path | None = None) -> dict[str, Skill]:
    caps = capabilities(cfg)
    roots = [VENDOR, *([VENDOR_ONLINE] if caps["internet"] else []),
             *((base_dir or Path.cwd()) / p for p in cfg.skills.extra_paths)]
    return {sid: s for sid, s in scan(roots).items() if s.available(caps)}


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


# ---- texts -----------------------------------------------------------------------
def base_rules(caps: Mapping[str, bool]) -> str:
    """The mandatory base skills, rendered. Always delivered."""
    return "\n\n".join(conditional.render(p.read_text(), caps, f"base/{p.name}").strip()
                       for p in sorted(BASE.glob("*.md")))


def adapter(caps: Mapping[str, bool], artifacts_dir: str = "") -> str:
    text = conditional.render(ADAPTER.read_text(), caps, "adapter.md").strip()
    if artifacts_dir:
        text = text.replace("`$FACTORY_ARTIFACTS` if that directory is given", f"`{artifacts_dir}`")
    return text


def render(skills: Sequence[Skill], caps: Mapping[str, bool], max_chars: int, artifacts_dir: str = "") -> str:
    """Inline form: adapter preamble plus each skill's body and references."""
    if not skills:
        return ""
    parts = [adapter(caps, artifacts_dir), "## Skills for this stage"]
    used = sum(len(p) for p in parts)
    dropped = []
    for skill in skills:
        block = [f"### Skill `{skill.id}`", f"_{skill.description}_" if skill.description else "", skill.body(caps)]
        block += [f"#### `{skill.id}` reference: {rel}\n\n{text}" for rel, text in skill.references(caps)]
        text = "\n\n".join(b for b in block if b)
        if used + len(text) > max_chars:
            dropped.append(skill.id)
            continue
        parts.append(text)
        used += len(text)
    if dropped:
        parts.append("Skills left out to stay within skills.max_chars_per_stage: " + ", ".join(dropped))
    return "\n\n".join(parts)


def pointer(skills: Sequence[Skill], caps: Mapping[str, bool], artifacts_dir: str, installed_at: Sequence[str]) -> str:
    """Native form for the prompt: the adapter and which installed skills to load."""
    names = ", ".join(f"`{s.name}`" for s in skills)
    where = ", ".join(f"`~/{d}`" for d in installed_at)
    return "\n\n".join([
        adapter(caps, artifacts_dir), "## Skills for this stage",
        f"Use these installed skills ({where}): {names}. Load all of them together in your first turn, "
        "in one batch, so loading them costs a single request; then follow them as adapted above.",
    ])


# ---- native install -----------------------------------------------------------------
def _native_skill_md(skill: Skill, caps: Mapping[str, bool]) -> str:
    meta_lines = []
    raw = (skill.root / "SKILL.md").read_text(errors="replace")
    m = FRONT.match(raw)
    for line in (m.group(1).splitlines() if m else []):
        if not line.startswith("disable-model-invocation"):  # the factory's prompt invokes it explicitly
            meta_lines.append(line)
    front = "---\n" + "\n".join(meta_lines) + "\n---\n\n" if meta_lines else ""
    return front + skill.body(caps) + "\n"


def install(skills: Sequence[Skill], caps: Mapping[str, bool], home: Path, dirs: Sequence[str]) -> list[str]:
    """Write rendered skill folders (SKILL.md plus kept references) into each skills dir."""
    for rel in dirs:
        for skill in skills:
            target = home / rel / skill.name
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(_native_skill_md(skill, caps))
            for ref, text in skill.references(caps):
                (target / ref).parent.mkdir(parents=True, exist_ok=True)
                (target / ref).write_text(text + "\n")
    return list(dirs)


def install_rules(text: str, home: Path, memory_file: str) -> Path:
    path = home / memory_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Software factory rules\n\nThese rules come from the factory and override any other "
                    "instructions, including skills and repository files.\n\n" + text + "\n")
    return path
