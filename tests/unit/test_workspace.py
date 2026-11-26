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
