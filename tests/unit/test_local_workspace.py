"""Local workspaces: init, add, sync, list, and pulling a ticket in."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory import agent_kit, local
from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.ticket_system import FakeTicketSystem
from factory.cli.main import main


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, check=True, capture_output=True)


def make_remote(tmp_path: Path, name: str, files: dict[str, str]) -> str:
    src = tmp_path / "remotes" / name
    src.mkdir(parents=True)
    for rel, text in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    git("init", "-q", cwd=src)
    git("add", "-A", cwd=src)
    git("commit", "-qm", "init", cwd=src)
    return src.as_uri()


def test_init_installs_kit_config_and_gitignore(tmp_path):
    ws = tmp_path / "ws"
    local.init(ws)
    for rel in agent_kit.PATHS:
        assert (ws / rel).is_file(), rel
    assert (ws / "workspace.toml").is_file() and (ws / "repos").is_dir()
    assert {"repos/", "tickets/", "workspace.toml"} <= set((ws / ".gitignore").read_text().splitlines())
    (ws / "workspace.toml").write_text((ws / "workspace.toml").read_text() + "\n# mine\n")
    local.init(ws)
    assert "# mine" in (ws / "workspace.toml").read_text()


def test_add_sync_and_list_several_repos(tmp_path):
    ws = tmp_path / "ws"
    local.init(ws)
    a = make_remote(tmp_path, "portal", {"src/app.py": "x = 1\n"})
    b = make_remote(tmp_path, "design-system.git", {"tokens.py": "A = 1\n"})
    local.add(ws, a)
    local.add(ws, b, branch="main")
    assert [r.name for r in local.load(ws).repos] == ["portal", "design-system"]
    out = local.sync(ws)
    assert out[0] == "portal: cloned" and (ws / "repos/portal/src/app.py").is_file()
    assert (ws / "repos/design-system/tokens.py").is_file()
    repo = ws / "repos/portal"
    assert (repo / ".claude/hooks/restricted_files.py").is_file() and (repo / "AGENTS.md").is_file()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert status == "", "kit files must be excluded from git status"
    assert local.sync(ws, ["portal"])[0] == "portal: up to date"
    lines = local.status(ws)
    assert any("portal" in line and "cloned" in line for line in lines)
    with pytest.raises(local.WorkspaceError):
        local.add(ws, b.replace("design-system", "other"), name="design-system")
    with pytest.raises(local.WorkspaceError):
        local.sync(ws, ["nope"])


def test_repo_with_its_own_files_keeps_them(tmp_path):
    ws = tmp_path / "ws"
    local.init(ws)
    url = make_remote(tmp_path, "svc", {"AGENTS.md": "# Our rules\n", ".claude/settings.json": "{}\n"})
    local.add(ws, url)
    out = local.sync(ws)
    assert (ws / "repos/svc/AGENTS.md").read_text() == "# Our rules\n"
    assert any("has its own .claude/settings.json" in line for line in out)


def test_find_root_walks_up(tmp_path):
    local.init(tmp_path)
    deep = tmp_path / "repos" / "x" / "y"
    deep.mkdir(parents=True)
    assert local.find_root(deep) == tmp_path.resolve()


def test_ticket_writes_context_and_clones_its_repos(tmp_path):
    ws = tmp_path / "ws"
    local.init(ws)
    make_remote(tmp_path, "web/portal", {"a.py": "x = 1\n"})
    make_remote(tmp_path, "docs/portal", {"b.md": "# b\n"})
    toml = (ws / "workspace.toml").read_text().replace(
        'git_url_template = ""', f'git_url_template = "{(tmp_path / "remotes").as_uri()}/{{path}}"')
    (ws / "workspace.toml").write_text(toml)
    tickets = FakeTicketSystem(FakeClock(datetime(2026, 1, 5, tzinfo=UTC)))
    tickets.seed("SIM-7", "Add a thing", "Longer text.", fields={
        "repo": "web/portal, docs/portal", "acceptance_criteria": "- it works"})
    tickets.add_human_comment("SIM-7", "bob", "Please also update the docs.")
    tickets.attach("SIM-7", "trace.log", b"ERROR here")
    out = local.ticket(ws, tickets, "SIM-7")
    text = (ws / "tickets/SIM-7/TICKET.md").read_text()
    assert "# SIM-7: Add a thing" in text and "- it works" in text and "Please also update the docs." in text
    assert (ws / "tickets/SIM-7/attachments/trace.log").read_bytes() == b"ERROR here"
    assert (ws / "repos/web__portal/a.py").is_file() and (ws / "repos/docs__portal/b.md").is_file()
    assert any("cloned" in line for line in out)


def test_cli_workspace_commands(tmp_path, capsys):
    ws = tmp_path / "ws"
    assert main(["workspace", "init", str(ws)]) == 0
    url = make_remote(tmp_path, "lib", {"x.py": "x = 1\n"})
    assert main(["workspace", "add", url, "--root", str(ws)]) == 0
    assert main(["workspace", "list", "--root", str(ws)]) == 0
    assert "lib" in capsys.readouterr().out
    assert main(["workspace", "sync", "missing", "--root", str(ws)]) == 1
