"""In-memory ticket system with realistic behavior: optional workflow
transition rules, no atomic claim, write hooks for simulating races,
injectable failures, idempotent creates and comments, and cursored reads."""

from __future__ import annotations

import dataclasses
import tomllib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

from factory.ports.context import AdapterContext
from factory.ports.errors import Conflict, InvalidRequest, NotFound
from factory.ports.filter_semantics import matches
from factory.ports.types import (
    Attachment,
    Comment,
    Cursor,
    LinkKind,
    Page,
    Principal,
    PrincipalKind,
    Ticket,
    TicketDraft,
    TicketFilter,
    TicketKey,
    TicketUpdate,
)

Hook = Callable[["FakeTicketSystem", str, TicketKey], None]


class FakeTicketSystem:
    NATIVE_ADAPTER = "fake"

    def __init__(self, clock, identity: str = "svc-factory", workflow: Mapping[str, Iterable[str]] | None = None) -> None:
        self._clock = clock
        self.identity = Principal(identity, identity, PrincipalKind.SERVICE)
        self.workflow = {k: set(v) for k, v in (workflow or {}).items()}
        self.tickets: dict[TicketKey, Ticket] = {}
        self.comments: dict[TicketKey, list[Comment]] = defaultdict(list)
        self.links: list[tuple[TicketKey, TicketKey, LinkKind]] = []
        self.writes: list[tuple[str, TicketKey, object]] = []
        self.reads: int = 0
        self._comment_keys: dict[tuple[TicketKey, str], Comment] = {}
        self._create_keys: dict[str, TicketKey] = {}
        self._counters: dict[str, int] = defaultdict(int)
        self._comment_seq = 0
        self._hooks: list[tuple[str, Hook]] = []
        self._failures: dict[str, list[Exception]] = defaultdict(list)
        self.attachment_data: dict[tuple[TicketKey, str], bytes] = {}

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> FakeTicketSystem:
        ts = cls(ctx.clock, str(settings.get("identity", "svc-factory")), settings.get("workflow"))
        fixtures = settings.get("fixtures")
        if fixtures and (ctx.base_dir / str(fixtures)).exists():
            with (ctx.base_dir / str(fixtures)).open("rb") as fh:
                for raw in tomllib.load(fh).get("tickets", []):
                    ts.seed(**raw)
        return ts

    # ---- test helpers -------------------------------------------------
    def seed(self, key: str, summary: str = "", description: str = "", status: str = "Open", project: str = "",
             issue_type: str = "Task", priority: str = "Medium", labels: Iterable[str] = (),
             components: Iterable[str] = (), reporter: str = "", assignee: str = "",
             fields: Mapping[str, object] | None = None, created: datetime | None = None,
             attachments: Iterable = ()) -> Ticket:
        now = created or self._clock.now()
        ticket = Ticket(
            key=key, project=project or key.split("-")[0], summary=summary, description=description,
            status=status, issue_type=issue_type, priority=priority, labels=frozenset(labels),
            components=frozenset(components), reporter=Principal(reporter, reporter) if reporter else None,
            assignee=Principal(assignee, assignee) if assignee else None, created=now, updated=now,
            fields=dict(fields or {}), attachments=tuple(attachments),
        )
        self.tickets[key] = ticket
        self._counters[ticket.project] = max(self._counters[ticket.project], _num(key))
        return ticket

    def add_human_comment(self, key: TicketKey, author: str, body: str, groups: Iterable[str] = (),
                          kind: PrincipalKind = PrincipalKind.USER) -> Comment:
        self._comment_seq += 1
        comment = Comment(f"c{self._comment_seq}", Principal(author, author, kind, frozenset(groups)), body,
                          self._clock.now())
        self.comments[key].append(comment)
        return comment

    def after(self, op: str, hook: Hook) -> None:
        """Run hook after the next write of kind op ('*' for any)."""
        self._hooks.append((op, hook))

    def fail_next(self, op: str, error: Exception, times: int = 1) -> None:
        self._failures[op].extend([error] * times)

    def comments_by_bot(self, key: TicketKey) -> list[Comment]:
        return [c for c in self.comments[key] if c.author.id == self.identity.id]

    def force_update(self, key: TicketKey, **changes: object) -> Ticket:
        ticket = dataclasses.replace(self.tickets[key], **changes, updated=self._clock.now())
        self.tickets[key] = ticket
        return ticket

    # ---- internals ----------------------------------------------------
    def _maybe_fail(self, op: str) -> None:
        if self._failures[op]:
            raise self._failures[op].pop(0)

    def _wrote(self, op: str, key: TicketKey, detail: object) -> None:
        if op != "search":
            self.writes.append((op, key, detail))
        pending = [(o, h) for o, h in self._hooks if o in (op, "*")]
        self._hooks = [(o, h) for o, h in self._hooks if o not in (op, "*")]
        for _, hook in pending:
            hook(self, op, key)

    def _get(self, key: TicketKey) -> Ticket:
        try:
            return self.tickets[key]
        except KeyError:
            raise NotFound(f"ticket {key}") from None

    def attach(self, key: TicketKey, filename: str, content: bytes, media_type: str = "text/plain") -> Attachment:
        att = Attachment(f"a{len(self.attachment_data) + 1}", filename, media_type, len(content))
        self.attachment_data[(key, att.id)] = content
        self.tickets[key] = dataclasses.replace(self.tickets[key], attachments=(*self.tickets[key].attachments, att))
        return att

    # ---- port ---------------------------------------------------------
    def get_attachment(self, key: TicketKey, attachment_id: str) -> bytes:
        self._maybe_fail("get_attachment")
        try:
            return self.attachment_data[(key, attachment_id)]
        except KeyError:
            raise NotFound(f"attachment {attachment_id} on {key}") from None

    def validate_filter(self, filter: TicketFilter) -> None:
        if filter.native is not None and filter.native.adapter != self.NATIVE_ADAPTER:
            raise InvalidRequest(f"native query for adapter {filter.native.adapter!r} cannot run on {self.NATIVE_ADAPTER!r}")
        if filter.native is not None:
            raise InvalidRequest("the fake ticket system has no native query language")

    def search(self, filter: TicketFilter, cursor: Cursor | None = None, limit: int = 50) -> Page[Ticket]:
        self._maybe_fail("search")
        self.validate_filter(filter)
        self.reads += 1
        hits = sorted((t for t in self.tickets.values() if matches(filter, t)), key=lambda t: (t.created, t.key))
        start = int(cursor or 0)
        page = hits[start:start + limit]
        nxt = str(start + limit) if start + limit < len(hits) else None
        self._wrote("search", "", filter)
        return Page(tuple(page), nxt)

    def get(self, key: TicketKey) -> Ticket:
        self._maybe_fail("get")
        self.reads += 1
        return self._get(key)

    def create(self, draft: TicketDraft) -> Ticket:
        self._maybe_fail("create")
        if draft.idempotency_key and draft.idempotency_key in self._create_keys:
            return self.tickets[self._create_keys[draft.idempotency_key]]
        self._counters[draft.project] += 1
        key = f"{draft.project}-{self._counters[draft.project]}"
        now = self._clock.now()
        ticket = Ticket(key, draft.project, draft.summary, draft.description, "Open", draft.issue_type, "Medium",
                        frozenset(draft.labels), frozenset(draft.components), draft.reporter, None, now, now,
                        dict(draft.fields))
        self.tickets[key] = ticket
        if draft.idempotency_key:
            self._create_keys[draft.idempotency_key] = key
        self._wrote("create", key, draft)
        return ticket

    def update(self, key: TicketKey, update: TicketUpdate) -> Ticket:
        self._maybe_fail("update")
        ticket = self._get(key)
        changes: dict[str, object] = {"fields": {**ticket.fields, **update.fields}, "updated": self._clock.now()}
        if update.summary is not None:
            changes["summary"] = update.summary
        if update.description is not None:
            changes["description"] = update.description
        if update.assignee is not None:
            changes["assignee"] = update.assignee if update.assignee.id else None
        self.tickets[key] = dataclasses.replace(ticket, **changes)
        self._wrote("update", key, update)
        return self.tickets[key]

    def transition(self, key: TicketKey, to_status: str) -> Ticket:
        self._maybe_fail("transition")
        ticket = self._get(key)
        if ticket.status == to_status:
            return ticket
        allowed = self.workflow.get(ticket.status)
        if allowed is not None and to_status not in allowed:
            raise Conflict(f"no transition from {ticket.status!r} to {to_status!r}")
        self.tickets[key] = dataclasses.replace(ticket, status=to_status, updated=self._clock.now())
        self._wrote("transition", key, to_status)
        return self.tickets[key]

    def add_comment(self, key: TicketKey, body: str, idempotency_key: str) -> Comment:
        self._maybe_fail("comment")
        self._get(key)
        existing = self._comment_keys.get((key, idempotency_key))
        if existing is not None:
            return existing
        self._comment_seq += 1
        comment = Comment(f"c{self._comment_seq}", self.identity, body, self._clock.now())
        self.comments[key].append(comment)
        self._comment_keys[(key, idempotency_key)] = comment
        self._wrote("comment", key, body)
        return comment

    def edit_comment(self, key: TicketKey, comment_id: str, body: str) -> Comment:
        self._maybe_fail("edit_comment")
        for i, comment in enumerate(self.comments[key]):
            if comment.id == comment_id:
                if comment.author.id != self.identity.id:
                    raise Conflict("cannot edit another user's comment")
                edited = dataclasses.replace(comment, body=body, updated=self._clock.now())
                self.comments[key][i] = edited
                self._wrote("edit_comment", key, body)
                return edited
        raise NotFound(f"comment {comment_id} on {key}")

    def list_comments(self, key: TicketKey, cursor: Cursor | None = None) -> Page[Comment]:
        self._maybe_fail("list_comments")
        self._get(key)
        self.reads += 1
        items = self.comments[key]
        start = next((i + 1 for i, c in enumerate(items) if c.id == cursor), 0) if cursor else 0
        page = tuple(items[start:])
        return Page(page, page[-1].id if page else cursor)

    def link(self, source: TicketKey, target: TicketKey, kind: LinkKind) -> None:
        self._maybe_fail("link")
        self._get(source)
        self._get(target)
        if (source, target, kind) not in self.links:
            self.links.append((source, target, kind))
            self._wrote("link", source, (target, kind))

    def set_labels(self, key: TicketKey, add: Iterable[str] = (), remove: Iterable[str] = ()) -> Ticket:
        self._maybe_fail("labels")
        ticket = self._get(key)
        labels = (set(ticket.labels) | set(add)) - set(remove)
        self.tickets[key] = dataclasses.replace(ticket, labels=frozenset(labels), updated=self._clock.now())
        self._wrote("labels", key, (tuple(add), tuple(remove)))
        return self.tickets[key]


def _num(key: str) -> int:
    tail = key.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0
