"""TicketSystem contract, run against the fake (real adapters join with fixtures in Phase 5)."""

from datetime import UTC, datetime

import pytest

from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.ticket_system import FakeTicketSystem
from factory.ports.errors import Conflict, InvalidRequest, NotFound
from factory.ports.types import LinkKind, NativeQuery, Principal, TicketDraft, TicketFilter, TicketUpdate


@pytest.fixture
def ts():
    t = FakeTicketSystem(FakeClock(datetime(2026, 1, 5, tzinfo=UTC)), workflow={"Open": ["Ready"], "Ready": ["Open"]})
    t.seed("SIM-1", "a", status="Open", labels=["x", "y"], components=["ui"], priority="High", fields={"repo": "r1"})
    t.seed("SIM-2", "b", status="Ready", labels=["x"], components=["docs"], issue_type="Bug", fields={"repo": "r2"})
    return t


def keys(page):
    return [t.key for t in page.items]


def test_neutral_filter_semantics(ts):
    assert keys(ts.search(TicketFilter(labels_any=frozenset({"y"})))) == ["SIM-1"]
    assert keys(ts.search(TicketFilter(labels_all=frozenset({"x", "y"})))) == ["SIM-1"]
    assert keys(ts.search(TicketFilter(labels_none=frozenset({"y"})))) == ["SIM-2"]
    assert keys(ts.search(TicketFilter(components=frozenset({"docs"})))) == ["SIM-2"]
    assert keys(ts.search(TicketFilter(issue_types=frozenset({"Bug"})))) == ["SIM-2"]
    assert keys(ts.search(TicketFilter(priorities=frozenset({"High"})))) == ["SIM-1"]
    assert keys(ts.search(TicketFilter(statuses=frozenset({"Ready"})))) == ["SIM-2"]
    assert keys(ts.search(TicketFilter(custom_fields={"repo": frozenset({"r1"})}))) == ["SIM-1"]


def test_pagination(ts):
    first = ts.search(TicketFilter(), limit=1)
    second = ts.search(TicketFilter(), first.next_cursor, limit=1)
    assert keys(first) + keys(second) == ["SIM-1", "SIM-2"] and second.next_cursor is None


def test_native_query_is_adapter_specific(ts):
    with pytest.raises(InvalidRequest):
        ts.validate_filter(TicketFilter(native=NativeQuery("jira", "project = X")))


def test_idempotent_create_and_comment(ts):
    d = TicketDraft("SIM", "new", "", "Task", Principal("alice"), idempotency_key="mail:1")
    assert ts.create(d).key == ts.create(d).key
    c1 = ts.add_comment("SIM-1", "hello", "k1")
    c2 = ts.add_comment("SIM-1", "hello", "k1")
    assert c1.id == c2.id and len(ts.comments["SIM-1"]) == 1


def test_comment_cursor_returns_only_newer(ts):
    a = ts.add_comment("SIM-1", "one", "k1")
    ts.add_human_comment("SIM-1", "bob", "two")
    page = ts.list_comments("SIM-1", a.id)
    assert [c.body for c in page.items] == ["two"]
    assert ts.list_comments("SIM-1", page.next_cursor).items == ()


def test_workflow_rules_and_errors(ts):
    with pytest.raises(Conflict):
        ts.transition("SIM-1", "Done")
    assert ts.transition("SIM-1", "Ready").status == "Ready"
    with pytest.raises(NotFound):
        ts.get("SIM-99")
    with pytest.raises(Conflict):
        ts.edit_comment("SIM-1", ts.add_human_comment("SIM-1", "bob", "x").id, "y")


def test_update_labels_links_and_failures(ts):
    ts.update("SIM-1", TicketUpdate(fields={"lease_owner": "w1"}, assignee=Principal("svc")))
    t = ts.get("SIM-1")
    assert t.fields["lease_owner"] == "w1" and t.fields["repo"] == "r1" and t.assignee.id == "svc"
    assert ts.set_labels("SIM-1", add=["z"], remove=["x"]).labels == frozenset({"y", "z"})
    ts.link("SIM-1", "SIM-2", LinkKind.RELATES)
    ts.link("SIM-1", "SIM-2", LinkKind.RELATES)
    assert len(ts.links) == 1
    ts.fail_next("get", TimeoutError("simulated"))
    with pytest.raises(TimeoutError):
        ts.get("SIM-1")
    assert ts.get("SIM-1").key == "SIM-1"
