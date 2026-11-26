import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from factory.config.schema import CapabilitiesConfig, FactoryConfig, SkillRef, SkillsConfig, StageSkills
from factory.core import skills
from factory.ports.types import Ticket

VENDOR = skills.VENDOR


def ticket(issue_type="Task") -> Ticket:
    t = datetime(2026, 1, 5, tzinfo=UTC)
    return Ticket("SIM-1", "SIM", "s", "", "Ready", issue_type, "Medium", frozenset(), frozenset(), None, None, t, t)


def test_every_vendored_skill_parses_and_is_licensed():
    for root in (VENDOR, skills.VENDOR_ONLINE):
        found = skills.scan([root])
        sources = tomllib.loads((root / "SOURCES.toml").read_text())
        for pack, meta in sources.items():
            assert (root / meta["license_file"]).is_file(), pack
            assert len(meta["commit"]) == 40, pack
            assert sorted(meta["skills"]) == sorted(s.split("/")[1] for s in found if s.startswith(pack + "/")), pack
            for rel in meta.get("modified", []):
                assert "Modified by software-factory" in (root / pack / rel).read_text(), rel
        for caps in ({"subagents": False, "internet": False}, {"subagents": True, "internet": True}):
            assert all(s.body(caps) and "factory:if" not in s.body(caps) for s in found.values())
    assert (VENDOR / "humanlayer-rpi" / "NOTICE").is_file()


def test_no_executables_or_scripts_are_vendored():
    for path in [*VENDOR.rglob("*"), *skills.VENDOR_ONLINE.rglob("*")]:
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


OFF = {"subagents": False, "internet": False}
ON = {"subagents": True, "internet": True}


def test_render_inlines_references_and_respects_the_budget(tmp_path):
    tdd = skills.scan([VENDOR])["mattpocock/tdd"]
    text = skills.render([tdd], OFF, 100_000, artifacts_dir="/ws/.factory/artifacts")
    assert text.startswith("# How to use the stage skills in the factory")
    assert "`/ws/.factory/artifacts`" in text and "### Skill `mattpocock/tdd`" in text
    assert "reference: mocking.md" in text and "One session, no sub-agents" in text and "Offline" in text
    assert "One session, no sub-agents" not in skills.render([tdd], ON, 100_000)
    small = skills.render([tdd], OFF, 3000)
    assert "left out to stay within skills.max_chars_per_stage: mattpocock/tdd" in small
    pointer = skills.pointer([tdd], OFF, "/n", [".claude/skills"])
    assert "`tdd`" in pointer and "`~/.claude/skills`" in pointer and "Load all of them together" in pointer


def test_capability_sidecars_and_online_pack():
    arch = skills.scan([VENDOR])["mattpocock/improve-codebase-architecture"]
    assert "HTML-REPORT.md" not in [r for r, _ in arch.references(OFF)]
    assert "HTML-REPORT.md" in [r for r, _ in arch.references(ON)]
    assert "Present candidates as a Markdown report" in arch.body(OFF)
    assert "cdn." not in arch.body(OFF) + "".join(t for _, t in arch.references(OFF))
    offline = FactoryConfig()
    online = FactoryConfig(capabilities=CapabilitiesConfig(internet=True))
    assert "mattpocock/software-factory" not in skills.catalog(offline)
    assert "mattpocock/software-factory" in skills.catalog(online)


def test_base_rules_follow_capabilities():
    off, on = skills.base_rules(OFF), skills.base_rules(ON)
    assert "Do not spawn sub-agents" in off and "There is no internet access" in off
    assert "Sub-agents are allowed but cost requests" in on and "There is no internet access" not in on
    assert "factory:if" not in off + on and "request" in off


def test_install_writes_rendered_skill_folders_into_home(tmp_path):
    found = skills.scan([VENDOR])
    chosen = [found["mattpocock/tdd"], found["mattpocock/code-review"]]
    assert skills.install(chosen, OFF, tmp_path, [".claude/skills", ".codex/skills"]) == [".claude/skills", ".codex/skills"]
    review = (tmp_path / ".claude/skills/code-review/SKILL.md").read_text()
    assert review.startswith("---\nname: code-review") and "factory:if" not in review
    assert "Review both axes yourself" in review and "Spawn both sub-agents" not in review
    assert "disable-model-invocation" not in review
    assert (tmp_path / ".codex/skills/tdd/mocking.md").is_file()
    rules = skills.install_rules(skills.base_rules(OFF), tmp_path, ".claude/CLAUDE.md")
    assert "override any other instructions" in rules.read_text()


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
