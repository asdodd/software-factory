from factory.core import workspace


def test_same_size_edit_in_the_same_second_is_detected(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "calc.py").write_text("return a * b\n")
    copy = tmp_path / "work"
    workspace.isolate(clone, copy)
    (copy / "calc.py").write_text("return b * a\n")
    found = workspace.changes(copy)
    assert [(c.status, c.path) for c in found] == [("M", "calc.py")]


def test_apply_and_stage_detects_same_size_edit_in_clone(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    git = workspace.local_git()
    git.run(["init", "-q"], cwd=clone)
    (clone / "calc.py").write_text("return a * b\n")
    git.run(["add", "-A"], cwd=clone)
    git.run(["commit", "-q", "-m", "init"], cwd=clone)
    copy = tmp_path / "work"
    workspace.isolate(clone, copy)
    (copy / "calc.py").write_text("return b * a\n")
    found = workspace.changes(copy)
    workspace.stage_all(git, clone, workspace.apply(found, copy, clone))
    assert git.out(["diff", "--cached", "--name-only"], cwd=clone) == "calc.py"


def test_repo_dirs_use_last_segment_unless_they_collide():
    assert workspace.repo_dirs(["web/portal", "web/design-system"]) == {"web/portal": "portal",
                                                                          "web/design-system": "design-system"}
    assert workspace.repo_dirs(["web/portal", "docs/portal", "x/y"]) == {"web/portal": "web__portal",
                                                                           "docs/portal": "docs__portal", "x/y": "y"}


def test_ticket_repos_accepts_one_or_several():
    from datetime import UTC, datetime

    from factory.core.readiness import ticket_repos
    from factory.ports.types import Ticket

    def ticket(value):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return Ticket("S-1", "S", "", "", "Open", "Task", "Medium", frozenset(), frozenset(), None, None, now, now,
                      {"repo": value})

    assert ticket_repos(ticket("web/portal")) == ["web/portal"]
    assert ticket_repos(ticket("web/portal, docs/handbook\nweb/portal")) == ["web/portal", "docs/handbook"]
    assert ticket_repos(ticket(["a/b", "c/d"])) == ["a/b", "c/d"]
    assert ticket_repos(ticket("- a/b\n- c/d")) == ["a/b", "c/d"]
    assert ticket_repos(ticket("")) == []


def test_isolate_runs_before_baseline_hook_outside_the_diff(tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "a.txt").write_text("a\n")
    copy = tmp_path / "copy"
    workspace.isolate(clone, copy, lambda dest: (dest / "KIT.md").write_text("kit\n"))
    assert (copy / "KIT.md").is_file()
    assert workspace.changes(copy) == []
