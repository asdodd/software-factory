"""Shared GitHost contract for merge-request and comment operations: identical
tests against the fake host and the real GitLab adapter (on the emulator).
Clone, pull, and submodules use local repositories for both."""

from datetime import UTC, datetime

import pytest

from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.git_host import FakeGitHost
from factory.adapters.gitlab.git_host import GitlabGitHost
from factory.adapters.http import HttpClient
from factory.config.schema import FactoryConfig
from factory.ports.types import MergeRequestDraft, MergeRequestState, RepoRef, Suggestion
from factory.vcs.git import Git, options_from_config
from tests.fixtures.gitlab_emulator import GitlabEmulator

REPO = RepoRef("", "web/app", "main")


class FakeHarness:
    def __init__(self, tmp_path):
        self.host = FakeGitHost(tmp_path / "repos", Git(options_from_config(FactoryConfig(), allow_protocols="file")),
                                FakeClock(datetime(2026, 1, 5, tzinfo=UTC)))
        self.repos = self.host

    def reviewer_comment(self, mr_id, author, body, thread=None):
        return self.host.add_mr_comment(mr_id, author, body, thread).thread_id

    def comment_event(self, author, body):
        self.host.add_comment_event("web/app", author, body)

    def set_state(self, mr_id, state):
        self.host.set_mr_state(mr_id, MergeRequestState(state))

    def thread_bodies(self, mr_id):
        return [c.body for c in self.host.mr_comments[mr_id]]


class GitlabHarness:
    def __init__(self, tmp_path):
        self.repos = FakeHarness(tmp_path).host
        self.em = GitlabEmulator()
        http = HttpClient(self.em.api_url, {"PRIVATE-TOKEN": "glpat-test-token-000000"}, self.em, sleep=lambda s: None)
        git = Git(options_from_config(FactoryConfig(), allow_protocols="file"))
        self.host = GitlabGitHost(http, git, "https://gitlab.sim.invalid", (tmp_path / "repos").as_uri(),
                                  ["web/app"], "svc-factory")

    def reviewer_comment(self, mr_id, author, body, thread=None):
        return self.em.reviewer_comment("web/app", int(mr_id), author, body, thread)

    def comment_event(self, author, body):
        self.em.comment_event("web/app", author, body)

    def set_state(self, mr_id, state):
        self.em.set_state("web/app", int(mr_id), {"open": "opened"}.get(state, state))

    def thread_bodies(self, mr_id):
        return [n["body"] for d in self.em.discussions[("web/app", int(mr_id))] for n in d["notes"]]


@pytest.fixture(params=["fake", "gitlab"])
def h(request, tmp_path):
    h = FakeHarness(tmp_path) if request.param == "fake" else GitlabHarness(tmp_path)
    h.repos.create_repo("web/app", {"app.py": "A = 1\n"})
    return h


def open_mr(h, tmp_path, key="k1"):
    ws = tmp_path / "ws"
    h.host.clone(REPO, ws)
    h.host.create_branch(ws, "ai/bot/SIM-1", "HEAD")
    (ws / "new.py").write_text("N = 1\n")
    h.repos.git.commit_all(ws, "change")
    h.host.push_branch(ws, "ai/bot/SIM-1")
    return h.host.open_merge_request(MergeRequestDraft(REPO, "ai/bot/SIM-1", "main", "t", "desc", key))


def test_clone_push_and_idempotent_mr(h, tmp_path):
    mr = open_mr(h, tmp_path)
    again = h.host.open_merge_request(MergeRequestDraft(REPO, "ai/bot/SIM-1", "main", "t", "desc", "k1"))
    assert mr.id == again.id and mr.state is MergeRequestState.OPEN and mr.source_branch == "ai/bot/SIM-1"
    assert "ai/bot/SIM-1" in h.repos.branches("web/app")
    assert h.host.get_merge_request(REPO, mr.id).state is MergeRequestState.OPEN
    h.set_state(mr.id, "merged")
    assert h.host.get_merge_request(REPO, mr.id).state is MergeRequestState.MERGED


def test_comment_cursor_and_idempotent_replies(h, tmp_path):
    mr = open_mr(h, tmp_path)
    thread = h.reviewer_comment(mr.id, "bob", "Why?")
    page = h.host.list_mr_comments(REPO, mr.id)
    assert [(c.author.id, c.body, c.thread_id) for c in page.items] == [("bob", "Why?", thread)]
    h.host.reply_in_thread(REPO, mr.id, thread, "Because.", "r1")
    h.host.reply_in_thread(REPO, mr.id, thread, "Because.", "r1")
    assert sum("Because." in b for b in h.thread_bodies(mr.id)) == 1
    h.reviewer_comment(mr.id, "bob", "Thanks", thread)
    newer = h.host.list_mr_comments(REPO, mr.id, page.next_cursor)
    assert "Thanks" in [c.body for c in newer.items]


def test_suggestions_are_idempotent(h, tmp_path):
    mr = open_mr(h, tmp_path)
    s = Suggestion("new.py", 1, 1, "N = 2", "Use 2.")
    h.host.post_suggestion(REPO, mr.id, s, "s1")
    h.host.post_suggestion(REPO, mr.id, s, "s1")
    bodies = h.thread_bodies(mr.id)
    assert sum("```suggestion" in b for b in bodies) == 1 and any("N = 2" in b for b in bodies)


def test_comment_events_cursor_and_replies(h, tmp_path):
    h.comment_event("bob", "/followup fix it")
    page = h.host.poll_comment_events()
    assert [(e.author.id, e.body) for e in page.items] == [("bob", "/followup fix it")]
    assert h.host.poll_comment_events(page.next_cursor).items == ()
    event = page.items[0]
    h.host.comment_on(event, "Created SIM-1", "c1")
    h.host.comment_on(event, "Created SIM-1", "c1")


def test_gitlab_specifics(tmp_path):
    h = GitlabHarness(tmp_path)
    h.repos.create_repo("web/app", {"app.py": "A = 1\n"})
    mr = open_mr(h, tmp_path)
    h.em.system_note("web/app", int(mr.id))
    assert h.host.list_mr_comments(REPO, mr.id).items == ()
    assert h.em.tokens == {"glpat-test-token-000000"}
    s = Suggestion("new.py", 1, 1, "N = 2", "Use 2.")
    h.host.post_suggestion(REPO, mr.id, s, "s1")
    posted = [b for m, p, b in h.em.requests if m == "POST" and p.endswith("/discussions")][0]
    assert posted["position"]["new_path"] == "new.py" and posted["position"]["head_sha"] == "b" * 40
    assert "<!-- factory-op:s1 -->" in posted["body"]
    h.comment_event("bob", "/followup x")
    event = h.host.poll_comment_events().items[0]
    h.host.comment_on(event, "Created SIM-1", "c1")
    h.host.comment_on(event, "Created SIM-1", "c1")
    assert len(h.em.target_notes[("web/app", "merge_requests", 1)]) == 1
    assert h.host.compare_url(REPO, "main", "ai/bot/SIM-1") == \
        "https://gitlab.sim.invalid/web/app/-/compare/main...ai%2Fbot%2FSIM-1"
