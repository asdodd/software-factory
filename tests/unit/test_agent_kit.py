"""The agent kit: the restricted-files hook, install/verify, and the copies at the repo root."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory import agent_kit

ROOT = Path(__file__).resolve().parents[2]
HOOK = agent_kit.KIT_ROOT / ".claude/hooks/restricted_files.py"
MARK = "propri" + "etary"  # built from parts so this file does not carry the marker itself


def run_hook(cwd: Path, tool: str, tool_input: dict, fmt: str = "claude", env: dict | None = None):
    if fmt == "cline":
        payload = {"hookName": "PreToolUse", "workspaceRoots": [str(cwd)],
                   "preToolUse": {"toolName": tool, "parameters": tool_input}}
        args = [sys.executable, str(HOOK), "--format", "cline"]
    else:
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}
        args = [sys.executable, str(HOOK)]
    return subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True, cwd=cwd,
                          env={"PATH": "/usr/bin:/bin", **(env or {})})


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "src/vendor.py").write_text(f"# {MARK.upper()} - do not distribute\nSECRET = 1\n")
    (tmp_path / MARK).mkdir()
    (tmp_path / MARK / "algo.py").write_text("SECRET = 2\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("# Guide\n")
    (tmp_path / "src/link.py").symlink_to(f"../{MARK}/algo.py")
    agent_kit.install(tmp_path)
    return tmp_path


def blocked(proc) -> bool:
    return proc.returncode == 2


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": f"{MARK}/algo.py"}),
    ("Read", {"file_path": "src/vendor.py"}),
    ("Read", {"file_path": "src/link.py"}),
    ("Edit", {"file_path": "src/vendor.py", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "src/vendor.py", "content": "x"}),
    ("Write", {"file_path": f"notes-{MARK.title()}.md", "content": "x"}),
    ("Grep", {"pattern": "SECRET"}),
    ("Grep", {"pattern": "SECRET", "path": "src"}),
    ("Grep", {"pattern": MARK, "path": "docs"}),
    ("Glob", {"pattern": f"**/*{MARK}*"}),
    ("Bash", {"command": "cat src/vendor.py"}),
    ("Bash", {"command": "cat src/*.py"}),
    ("Bash", {"command": "grep -rn SECRET ."}),
    ("Bash", {"command": "rg SECRET"}),
    ("Bash", {"command": "git show HEAD:src/vendor.py"}),
    ("Bash", {"command": "git diff"}),
    ("Bash", {"command": f"ls {MARK.upper()}"}),
])
def test_hook_blocks_restricted_reads(repo, tool, tool_input):
    proc = run_hook(repo, tool, tool_input)
    assert blocked(proc), proc.stderr
    assert "restricted-files" in proc.stderr
    assert "SECRET" not in proc.stderr


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "src/calc.py"}),
    ("Read", {"file_path": "AGENTS.md"}),
    ("Read", {"file_path": "CLAUDE.md"}),
    ("Edit", {"file_path": "src/calc.py", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "src/new.py", "content": "x = 1\n"}),
    ("Grep", {"pattern": "def add", "path": "docs"}),
    ("Grep", {"pattern": "def add", "path": "src/calc.py"}),
    ("Glob", {"pattern": "src/**/*.py"}),
    ("Bash", {"command": "python3 -m unittest discover -s tests"}),
    ("Bash", {"command": "cat src/calc.py docs/guide.md"}),
    ("Bash", {"command": "grep -rn Guide docs"}),
    ("Bash", {"command": "git diff -- docs"}),
    ("Bash", {"command": "git status"}),
    ("WebFetch", {"url": "https://example.com"}),
])
def test_hook_allows_ordinary_work(repo, tool, tool_input):
    proc = run_hook(repo, tool, tool_input)
    assert proc.returncode == 0, proc.stderr


def test_blocked_search_names_paths_so_the_agent_can_narrow_it(repo):
    proc = run_hook(repo, "Grep", {"pattern": "SECRET"})
    assert "src/vendor.py" in proc.stderr and MARK in proc.stderr


def test_kit_files_do_not_block_searches_of_the_workspace(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    agent_kit.install(tmp_path)
    assert run_hook(tmp_path, "Grep", {"pattern": "x"}).returncode == 0
    assert run_hook(tmp_path, "Bash", {"command": "grep -rn x ."}).returncode == 0


def test_marker_outside_the_kit_section_still_counts(tmp_path):
    (tmp_path / "AGENTS.md").write_text(f"Team notes: {MARK} roadmap below.\n")
    agent_kit.install(tmp_path)
    assert blocked(run_hook(tmp_path, "Read", {"file_path": "AGENTS.md"}))


def test_extra_markers_add_to_the_base_one(repo):
    (repo / "src/export.py").write_text("# EXPORT-CONTROLLED\n")
    assert run_hook(repo, "Read", {"file_path": "src/export.py"}).returncode == 0
    assert blocked(run_hook(repo, "Read", {"file_path": "src/export.py"}, env={"RESTRICTED_MARKERS": "export-controlled"}))
    assert blocked(run_hook(repo, "Read", {"file_path": "src/vendor.py"}, env={"RESTRICTED_MARKERS": "other"}))


def test_cline_format(repo):
    proc = run_hook(repo, "read_file", {"path": "src/vendor.py"}, fmt="cline")
    assert proc.returncode == 0 and json.loads(proc.stdout)["cancel"] is True
    proc = run_hook(repo, "execute_command", {"command": "cat src/vendor.py"}, fmt="cline")
    assert json.loads(proc.stdout)["cancel"] is True
    proc = run_hook(repo, "read_file", {"path": "src/calc.py"}, fmt="cline")
    assert json.loads(proc.stdout)["cancel"] is False


def test_hook_fails_closed_on_bad_input(tmp_path):
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
    assert proc.returncode == 2


def test_kit_sources_do_not_carry_the_marker_outside_sentinels():
    for rel in agent_kit.PATHS[:-1]:
        path = agent_kit.KIT_ROOT / rel
        if path.is_file():
            text = agent_kit_region_stripped(path.read_text())
            assert MARK not in text.lower(), rel


def agent_kit_region_stripped(text: str) -> str:
    import re
    return re.sub(r"agent-kit:restricted-files:begin.*?agent-kit:restricted-files:end", "", text, flags=re.S)


def test_install_merges_existing_settings_and_notes(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(npm test)"], "deny": ["Read(.env)"]},
        "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt"}]}]}}))
    (tmp_path / "AGENTS.md").write_text("# Repo rules\nUse tabs.\n")
    digests = agent_kit.install(tmp_path)
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert settings["permissions"]["allow"] == ["Bash(npm test)"]
    assert settings["permissions"]["deny"][0] == "Read(.env)" and len(settings["permissions"]["deny"]) == 5
    assert settings["hooks"]["PostToolUse"] and "restricted_files.py" in json.dumps(settings["hooks"]["PreToolUse"])
    notes = (tmp_path / "AGENTS.md").read_text()
    assert notes.startswith("# Repo rules") and agent_kit.SENTINEL in notes
    assert agent_kit.install(tmp_path) == digests  # idempotent
    assert agent_kit.verify(tmp_path, digests) == []
    (tmp_path / ".claude/settings.json").write_text("{}")
    (tmp_path / ".clinerules/hooks/PreToolUse").unlink()
    assert agent_kit.verify(tmp_path, digests) == [".claude/settings.json", ".clinerules/hooks/PreToolUse"]


def test_repo_root_carries_the_same_kit():
    for rel in agent_kit.COPIED + (agent_kit.SETTINGS,):
        assert (ROOT / rel).read_bytes() == (agent_kit.KIT_ROOT / rel).read_bytes(), rel
    for rel in agent_kit.NOTES:
        assert (agent_kit.KIT_ROOT / "AGENTS.md").read_text().strip() in (ROOT / rel).read_text(), rel
