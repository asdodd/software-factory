from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from factory.ports.types import (
    Comment,
    Cursor,
    LinkKind,
    Page,
    Ticket,
    TicketDraft,
    TicketFilter,
    TicketKey,
    TicketUpdate,
)


@runtime_checkable
class TicketSystem(Protocol):
    """Jira or equivalent. Status, label, and field values are native strings
    that core resolves from config; logical field names map to native ids
    inside the adapter via its settings block."""

    def search(self, filter: TicketFilter, cursor: Cursor | None = None, limit: int = 50) -> Page[Ticket]: ...

    def get(self, key: TicketKey) -> Ticket: ...

    def create(self, draft: TicketDraft) -> Ticket:
        """Idempotent on draft.idempotency_key."""
        ...

    def update(self, key: TicketKey, update: TicketUpdate) -> Ticket: ...

    def transition(self, key: TicketKey, to_status: str) -> Ticket:
        """Move to a native status. The adapter finds the transition."""
        ...

    def add_comment(self, key: TicketKey, body: str, idempotency_key: str) -> Comment:
        """Never posts twice for the same idempotency_key."""
        ...

    def edit_comment(self, key: TicketKey, comment_id: str, body: str) -> Comment: ...

    def list_comments(self, key: TicketKey, cursor: Cursor | None = None) -> Page[Comment]:
        """Comments strictly after cursor, oldest first."""
        ...

    def link(self, source: TicketKey, target: TicketKey, kind: LinkKind) -> None: ...

    def set_labels(self, key: TicketKey, add: Iterable[str] = (), remove: Iterable[str] = ()) -> Ticket: ...

    def get_attachment(self, key: TicketKey, attachment_id: str) -> bytes:
        """Raw bytes."""
        ...

    def validate_filter(self, filter: TicketFilter) -> None:
        """Raise InvalidRequest if the filter (incl. native escape hatch) is unusable. No I/O."""
        ...
