"""Shared TicketSystem contract: identical tests against the fake and the real
Jira adapter (on the fixture emulator). A real adapter is acceptable only if it
passes the same suite as the fake."""

from datetime import UTC, datetime

import pytest

from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.ticket_system import FakeTicketSystem
from factory.adapters.http import HttpClient
from factory.adapters.jira.ticket_system import JiraTicketSystem
from factory.ports.errors import Conflict, InvalidRequest, NotFound
from factory.ports.types import LinkKind, NativeQuery, Principal, TicketDraft, TicketFilter, TicketUpdate
from tests.fixtures.jira_emulator import JiraEmulator

FIELDS = {"repo": "customfield_10003", "lease_owner": "customfield_10001", "source_ref": "customfield_10007"}


class FakeHarness:
    native = "fake"

    def __init__(self):
        self.ts = FakeTicketSystem(FakeClock(datetime(2026, 1, 5, tzinfo=UTC)),
                                   workflow={"Open": ["Ready"], "Ready": ["In Progress", "Open"], "In Progress": ["Done"]})

    def seed(self, key, summary="", status="Open", labels=(), components=(), issue_type="Task", priority="Medium",
             fields=None):
        self.ts.seed(key, summary, status=status, labels=labels, components=components, issue_type=issue_type,
                     priority=priority, fields=fields or {})

    def human_comment(self, key, author, body):
        self.ts.add_human_comment(key, author, body)

    def attach(self, key, name, data):
        return self.ts.attach(key, name, data).id

    def add_user(self, name, email, active=True):
        if active:
            self.ts.add_user(email, name)


class JiraHarness:
    native = "jira"

    def __init__(self):
        self.em = JiraEmulator(statuses=("Open", "Ready", "In Progress", "Done"),
                               transitions={"Open": ["Ready"], "Ready": ["In Progress", "Open"], "In Progress": ["Done"]},
                               groups={"bob": ["factory-reviewers"]})
        http = HttpClient(self.em.base_url, {"Authorization": "Bearer t0k3n-value"}, self.em, sleep=lambda s: None)
        self.ts = JiraTicketSystem(http, FIELDS, {"relates": "Relates"}, {}, {})

    def seed(self, key, summary="", status="Open", labels=(), components=(), issue_type="Task", priority="Medium",
             fields=None):
        self.em.seed(key, summary, status, labels, components, issue_type, priority,
                     fields={FIELDS[k]: v for k, v in (fields or {}).items()})

    def human_comment(self, key, author, body):
        self.em.human_comment(key, author, body)

    def attach(self, key, name, data):
        return self.em.attach(key, name, data)

    def add_user(self, name, email, active=True):
        self.em.users[name] = (email, active)


@pytest.fixture(params=["fake", "jira"])
def h(request):
    h = FakeHarness() if request.param == "fake" else JiraHarness()
    h.seed("SIM-1", "a", status="Open", labels=["x", "y"], components=["ui"], priority="High", fields={"repo": "r1"})
    h.seed("SIM-2", "b", status="Ready", labels=["x"], components=["docs"], issue_type="Bug", fields={"repo": "r2"})
    return h


def keys(page):
    return [t.key for t in page.items]


def test_neutral_filter_semantics(h):
    ts = h.ts
    assert keys(ts.search(TicketFilter(labels_any=frozenset({"y"})))) == ["SIM-1"]
    assert keys(ts.search(TicketFilter(labels_all=frozenset({"x", "y"})))) == ["SIM-1"]
    assert keys(ts.search(TicketFilter(labels_none=frozenset({"y"})))) == ["SIM-2"]
    assert keys(ts.search(TicketFilter(components=frozenset({"docs"})))) == ["SIM-2"]
    assert keys(ts.search(TicketFilter(issue_types=frozenset({"Bug"})))) == ["SIM-2"]
    assert keys(ts.search(TicketFilter(priorities=frozenset({"High"})))) == ["SIM-1"]
    assert keys(ts.search(TicketFilter(statuses=frozenset({"Ready"})))) == ["SIM-2"]
    assert keys(ts.search(TicketFilter(custom_fields={"repo": frozenset({"r1"})}))) == ["SIM-1"]


def test_pagination(h):
    first = h.ts.search(TicketFilter(), limit=1)
    second = h.ts.search(TicketFilter(), first.next_cursor, limit=1)
    assert keys(first) + keys(second) == ["SIM-1", "SIM-2"] and second.next_cursor is None


def test_native_query_only_on_its_own_adapter(h):
    other = "jira" if h.native == "fake" else "fake"
    with pytest.raises(InvalidRequest):
        h.ts.validate_filter(TicketFilter(native=NativeQuery(other, "x")))


def test_idempotent_create_and_comment(h):
    d = TicketDraft("SIM", "new", "desc", "Task", Principal("alice"), fields={"source_ref": "mail:1"},
                    idempotency_key="mail:1")
    first, again = h.ts.create(d), h.ts.create(d)
    assert first.key == again.key and first.summary == "new" and first.reporter.id == "alice"
    assert first.fields["source_ref"] == "mail:1"
    c1 = h.ts.add_comment("SIM-1", "hello", "k1")
    c2 = h.ts.add_comment("SIM-1", "hello", "k1")
    assert c1.id == c2.id and len(h.ts.list_comments("SIM-1").items) == 1


def test_comment_cursor_returns_only_newer(h):
    a = h.ts.add_comment("SIM-1", "one", "k1")
    h.human_comment("SIM-1", "bob", "two")
    page = h.ts.list_comments("SIM-1", a.id)
    assert [c.body for c in page.items] == ["two"] and page.items[0].author.id == "bob"
    assert h.ts.list_comments("SIM-1", page.next_cursor).items == ()
    edited = h.ts.edit_comment("SIM-1", a.id, "one, edited")
    assert edited.body == "one, edited"


def test_transitions_follow_the_workflow(h):
    with pytest.raises(Conflict):
        h.ts.transition("SIM-1", "Done")
    assert h.ts.transition("SIM-1", "Ready").status == "Ready"
    assert h.ts.transition("SIM-1", "Ready").status == "Ready"
    with pytest.raises(NotFound):
        h.ts.get("SIM-99")


def test_update_labels_links_attachments(h):
    t = h.ts.update("SIM-1", TicketUpdate(fields={"lease_owner": "w1"}, assignee=Principal("svc")))
    assert t.fields["lease_owner"] == "w1" and t.fields["repo"] == "r1" and t.assignee.id == "svc"
    assert h.ts.set_labels("SIM-1", add=["z"], remove=["x"]).labels == frozenset({"y", "z"})
    h.ts.link("SIM-1", "SIM-2", LinkKind.RELATES)
    aid = h.attach("SIM-1", "notes.txt", b"hello")
    assert [a.filename for a in h.ts.get("SIM-1").attachments] == ["notes.txt"]
    assert h.ts.get_attachment("SIM-1", aid) == b"hello"


def test_find_user_by_email_is_exact_and_active_only(h):
    h.add_user("alice", "Alice@Team.example")
    h.add_user("malice", "malice@team.example")
    h.add_user("gone", "gone@team.example", active=False)
    assert h.ts.find_user_by_email("alice@team.example").id == "alice"
    assert h.ts.find_user_by_email("ALICE@team.example").id == "alice"
    assert h.ts.find_user_by_email("lice@team.example") is None
    assert h.ts.find_user_by_email("gone@team.example") is None
    assert h.ts.find_user_by_email("nobody@team.example") is None


def test_jira_specifics():
    h = JiraHarness()
    h.seed("SIM-1", "a")
    h.human_comment("SIM-1", "bob", "hi")
    comment = h.ts.list_comments("SIM-1").items[0]
    assert comment.author.groups == frozenset({"factory-reviewers"})
    assert h.em.auth_seen == {"Bearer"}
    bodies = [b for m, p, b in h.em.requests if m == "POST" and p.endswith("/comment")]
    h.ts.add_comment("SIM-1", "status", "op-1")
    posted = [b for m, p, b in h.em.requests if m == "POST" and p.endswith("/comment")][len(bodies):]
    assert posted == [{"body": "status", "properties": [{"key": "factory", "value": {"op": "op-1"}}]}]
    with pytest.raises(InvalidRequest):
        h.ts.validate_filter(TicketFilter(native=NativeQuery("jira", 'project = "X')))
    h.ts.validate_filter(TicketFilter(native=NativeQuery("jira", 'fixVersion = "1.2"')))
