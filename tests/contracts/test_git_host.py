"""GitHost contract against the fake host, which runs real git on local repos."""

import pytest

from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.git_host import FakeGitHost
from factory.config.schema import FactoryConfig, GitConfig, GitSubmodulesConfig, UrlRewrite
from factory.ports.errors import DivergedClone, SubmoduleFailure
from factory.ports.types import MergeRequestDraft, RepoRef
from factory.vcs.git import Git, options_from_config


def host(tmp_path, **git):
    cfg = FactoryConfig(git=GitConfig(**git))
    return FakeGitHost(tmp_path / "repos", Git(options_from_config(cfg, allow_protocols="file")), FakeClock())


@pytest.fixture
def nested(tmp_path):
    h = host(tmp_path)
    h.create_repo("libs/deep", {"deep.py": "D = 1\n"})
    h.create_repo("libs/core", {"core.py": "C = 1\n"}, submodules={"deep": "../deep.git"})
    h.create_repo("web/app", {"app.py": "A = 1\n"}, submodules={"vendor/core": "../../libs/core.git"})
    return h


def test_clone_nested_relative_submodules_at_recorded_commits(nested, tmp_path):
    later = nested.commit("libs/core", {"later.py": "L = 1\n"})
    result = nested.clone(RepoRef("", "web/app", "main"), tmp_path / "ws")
    paths = {s.path: s for s in result.submodules}
    assert set(paths) == {"vendor/core", "vendor/core/deep"}
    assert all(s.ok and s.checked_out_commit == s.recorded_commit for s in result.submodules)
    assert paths["vendor/core"].recorded_commit != later
    assert (tmp_path / "ws/vendor/core/deep/deep.py").exists()
    assert not (tmp_path / "ws/vendor/core/later.py").exists()


def test_pull_fast_forwards_only(nested, tmp_path):
    ws = tmp_path / "ws"
    nested.clone(RepoRef("", "web/app", "main"), ws)
    nested.commit("web/app", {"app.py": "A = 2\n"})
    nested.pull(ws)
    assert (ws / "app.py").read_text() == "A = 2\n"


def test_diverged_clone_is_refused(nested, tmp_path):
    ws = tmp_path / "ws"
    nested.clone(RepoRef("", "web/app", "main"), ws)
    nested.git.run(["commit", "-q", "--allow-empty", "-m", "local"], cwd=ws)
    nested.commit("web/app", {"app.py": "A = 3\n"})
    with pytest.raises(DivergedClone):
        nested.pull(ws)
    assert (ws / "app.py").read_text() == "A = 1\n"


def test_unreachable_submodule_fails_or_continues(tmp_path):
    import shutil
    for mode, expect_error in (("fail", True), ("continue_without", False)):
        h = host(tmp_path / mode, submodules=GitSubmodulesConfig(on_failure=mode))
        h.create_repo("libs/gone", {"g.py": ""})
        h.create_repo("web/app", {"a.py": ""}, submodules={"vendor/gone": "../../libs/gone.git"})
        shutil.rmtree(h.repos_root / "libs/gone.git")
        ref = RepoRef("", "web/app", "main")
        if expect_error:
            with pytest.raises(SubmoduleFailure) as err:
                h.clone(ref, tmp_path / mode / "ws")
            assert err.value.path == "vendor/gone"
        else:
            result = h.clone(ref, tmp_path / mode / "ws")
            assert [(s.path, s.ok) for s in result.submodules] == [("vendor/gone", False)]


def test_url_rewrite_reaches_other_host(tmp_path):
    other = (tmp_path / "repos" / "other").as_uri() + "/"
    h = host(tmp_path, url_rewrites=[UrlRewrite("https://other-host.example/", other)])
    h.create_repo("other/shared", {"s.py": ""})
    h.create_repo("web/app", {"a.py": ""}, submodules={"vendor/shared": "https://other-host.example/shared.git"})
    result = h.clone(RepoRef("", "web/app", "main"), tmp_path / "ws")
    assert result.submodules[0].ok and result.submodules[0].url == "https://other-host.example/shared.git"


def test_submodules_disabled(nested, tmp_path):
    h = FakeGitHost(nested.repos_root, Git(options_from_config(
        FactoryConfig(git=GitConfig(submodules=GitSubmodulesConfig(enabled=False))), allow_protocols="file")),
        FakeClock())
    assert h.clone(RepoRef("", "web/app", "main"), tmp_path / "ws2").submodules == ()


def test_branch_push_and_idempotent_mr(nested, tmp_path):
    ws = tmp_path / "ws"
    repo = RepoRef("", "web/app", "main")
    nested.clone(repo, ws)
    nested.create_branch(ws, "ai/bot/SIM-1", "HEAD")
    (ws / "new.py").write_text("N = 1\n")
    nested.git.commit_all(ws, "change")
    nested.push_branch(ws, "ai/bot/SIM-1")
    assert "ai/bot/SIM-1" in nested.branches("web/app")
    draft = MergeRequestDraft(repo, "ai/bot/SIM-1", "main", "t", "d", "k1")
    assert nested.open_merge_request(draft).id == nested.open_merge_request(draft).id
