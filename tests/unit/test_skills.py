import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from factory.config.schema import FactoryConfig, SkillRef, SkillsConfig, StageSkills
from factory.core import skills
from factory.ports.types import Ticket

VENDOR = skills.VENDOR


def ticket(issue_type="Task") -> Ticket:
    t = datetime(2026, 1, 5, tzinfo=UTC)
    return Ticket("SIM-1", "SIM", "s", "", "Ready", issue_type, "Medium", frozenset(), frozenset(), None, None, t, t)


def test_every_vendored_skill_parses_and_is_licensed():
    found = skills.scan([VENDOR])
    sources = tomllib.loads((VENDOR / "SOURCES.toml").read_text())
    for pack, meta in sources.items():
        assert (VENDOR / meta["license_file"]).is_file(), pack
        assert len(meta["commit"]) == 40, pack
        assert sorted(meta["skills"]) == sorted(s.split("/")[1] for s in found if s.startswith(pack + "/")), pack
    assert (VENDOR / "humanlayer-rpi" / "NOTICE").is_file()
    assert all(s.body() for s in found.values())


def test_no_executables_or_scripts_are_vendored():
    for path in VENDOR.rglob("*"):
        assert path.name != "scripts" and path.suffix != ".sh", path
        if path.is_file():
            assert not os.access(path, os.X_OK), path


def test_selection_conditions():
    cfg = FactoryConfig(skills=SkillsConfig(stages=StageSkills(implement=[
        SkillRef("mattpocock/tdd"), SkillRef("mattpocock/diagnosing-bugs", issue_types=["Bug"]),
        SkillRef("humanlayer-rpi/implement-plan", requires_spec=True)])))
    ids = lambda t, spec: [s.id for s in skills.select(cfg, "implement", t, spec)]  # noqa: E731
    assert ids(ticket("Task"), False) == ["mattpocock/tdd"]
    assert ids(ticket("Bug"), True) == ["mattpocock/tdd", "mattpocock/diagnosing-bugs", "humanlayer-rpi/implement-plan"]
    off = FactoryConfig(skills=SkillsConfig(enabled=False, stages=cfg.skills.stages))
    assert skills.select(off, "implement", ticket(), True) == []


def test_render_inlines_references_and_respects_the_budget(tmp_path):
    tdd = skills.scan([VENDOR])["mattpocock/tdd"]
    text = skills.render([tdd], 100_000, artifacts_dir="/ws/.factory/artifacts", installed_at=[".claude/skills"])
    assert text.startswith("# How to use the stage skills in the factory")
    assert "`/ws/.factory/artifacts`" in text and "### Skill `mattpocock/tdd`" in text
    assert "reference: mocking.md" in text and "`~/.claude/skills`" in text
    small = skills.render([tdd], 3000)
    assert "left out to stay within skills.max_chars_per_stage: mattpocock/tdd" in small


def test_install_copies_skill_folders_into_home(tmp_path):
    tdd = skills.scan([VENDOR])["mattpocock/tdd"]
    assert skills.install([tdd], tmp_path, [".claude/skills", ".codex/skills"]) == [".claude/skills", ".codex/skills"]
    assert (tmp_path / ".claude/skills/tdd/SKILL.md").is_file() and (tmp_path / ".codex/skills/tdd/mocking.md").is_file()


def test_unknown_and_extra_skills(tmp_path):
    pack = tmp_path / "skills" / "acme" / "house-style"
    pack.mkdir(parents=True)
    (pack / "SKILL.md").write_text("---\nname: house-style\ndescription: Our style.\n---\nUse tabs.\n")
    cfg = FactoryConfig(skills=SkillsConfig(extra_paths=["skills"], stages=StageSkills(review=[
        SkillRef("acme/house-style"), SkillRef("nobody/missing")])))
    assert skills.unknown(cfg, tmp_path) == ["nobody/missing"]
    assert [s.id for s in skills.select(cfg, "review", None, False, tmp_path)] == ["acme/house-style"]


def test_base_config_defaults_reference_vendored_skills():
    root = Path(__file__).resolve().parents[2]
    base = tomllib.loads((root / "config" / "base.toml").read_text())
    known = skills.scan([VENDOR])
    for stage, refs in base["skills"]["stages"].items():
        for ref in refs:
            assert ref["skill"] in known, (stage, ref)
