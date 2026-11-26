import os

import pytest

from factory.access.guard import Policy, PolicyAccessGuard


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/ok.py").write_text("x = 1\n")
    (tmp_path / "src/marked.py").write_text("# Proprietary and confidential\n")
    (tmp_path / "proprietary").mkdir()
    (tmp_path / "proprietary/algo.py").write_text("secret\n")
    os.symlink("../proprietary/algo.py", tmp_path / "src/link.py")
    os.symlink("/etc/hostname", tmp_path / "src/outside.py")
    return tmp_path


def guard(mode="strip", **kw):
    return PolicyAccessGuard(Policy(mode=mode, **kw))


def test_strip_removes_matches_by_path_content_and_symlink(ws):
    g = guard()
    report = g.scan_workspace(ws)
    assert set(report.matched_paths) == {"proprietary", "src/marked.py", "src/link.py", "src/outside.py"}
    assert sorted(p.name for p in (ws / "src").iterdir()) == ["ok.py"]


def test_deny_read_hides_and_denies_every_route(ws):
    g = guard("deny_read")
    g.scan_workspace(ws)
    assert (ws / "src/marked.py").exists()
    denied = [
        g.check_path(ws, "proprietary/algo.py"),
        g.check_path(ws, "src/marked.py"),
        g.check_path(ws, "src/link.py"),
        g.check_path(ws, "src/../proprietary/algo.py"),
        g.check_path(ws, "../etc/passwd"),
        g.check_path(ws, ".git/config"),
        g.check_command(ws, ["cat", "src/marked.py"]),
        g.check_command(ws, ["grep", "-r", "x", "src"]),
        g.check_command(ws, ["git", "show", "HEAD:src/marked.py"]),
        g.check_command(ws, ["git", "log", "-p"]),
        g.check_command(ws, ["git", "cat-file", "-p", "abc123"]),
        g.check_command(ws, ["bash", "-c", "cat src/*"]),
        g.check_command(ws, ["python3", "-c", "print(open('src/marked.py').read())"]),
        g.check_command(ws, ["xargs", "cat"]),
        g.check_command(ws, ["make", "build"]),
    ]
    assert not any(d.allowed for d in denied)
    assert len(g.denials) == len(denied)
    assert g.check_path(ws, "src/ok.py").allowed
    assert g.check_command(ws, ["cat", "src/ok.py"]).allowed
    assert g.check_command(ws, ["git", "log", "-p", "--", "src/ok.py"]).allowed
    assert not g.is_visible(ws, "src/marked.py") and g.is_visible(ws, "src/ok.py")


def test_strip_mode_allows_ordinary_commands_but_denies_named_paths(ws):
    g = guard()
    g.scan_workspace(ws)
    assert g.check_command(ws, ["python3", "-m", "pytest"]).allowed
    assert g.check_command(ws, ["git", "log", "-p"]).allowed
    assert not g.check_command(ws, ["cat", "PROPRIETARY/algo.py"]).allowed


def test_attachments_output_and_diff(ws):
    g = guard()
    assert not g.check_attachment("Proprietary-spec.pdf", b"x").allowed
    assert not g.check_attachment("notes.txt", b"PROPRIETARY notice").allowed
    assert g.check_attachment("notes.txt", b"fine").allowed
    out = g.filter_output("a\nsee proprietary/algo.py\nb")
    assert out.redactions == 1 and "proprietary" not in out.text
    assert not g.check_diff_paths(["src/a.py", "lib/proprietary.c"]).allowed


def test_derive_only_adds_patterns(ws):
    g = guard().derive(["export-controlled"])
    assert g.policy.patterns == ("proprietary", "export-controlled")
    assert not g.check_path(ws, "docs/Export-Controlled.md").allowed


def test_case_sensitivity_and_disabled_policy(ws):
    assert guard(case_insensitive=False).check_path(ws, "PROPRIETARY/x").allowed
    assert PolicyAccessGuard(Policy(enabled=False)).check_path(ws, "proprietary/algo.py").allowed
