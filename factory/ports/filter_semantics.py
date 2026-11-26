"""Reference semantics of the neutral TicketFilter. Fakes evaluate filters
with this; real adapters translate to native queries that must agree with it
(the contract suite checks both)."""

from __future__ import annotations

from collections.abc import Iterable

from factory.ports.types import Ticket, TicketFilter


def _norm(values: Iterable[object]) -> set[str]:
    out: set[str] = set()
    for value in values:
        if isinstance(value, (list, tuple, set, frozenset)):
            out.update(str(v) for v in value)
        elif value is not None and value != "":
            out.add(str(value))
    return out


def matches(f: TicketFilter, ticket: Ticket) -> bool:
    if f.native is not None:
        raise ValueError("native queries cannot be evaluated by the neutral semantics")
    labels = set(ticket.labels)
    if f.labels_any and not (labels & f.labels_any):
        return False
    if f.labels_all and not f.labels_all <= labels:
        return False
    if labels & f.labels_none:
        return False
    if f.projects and ticket.project not in f.projects:
        return False
    if f.components and not (set(ticket.components) & f.components):
        return False
    if f.issue_types and ticket.issue_type not in f.issue_types:
        return False
    if f.priorities and ticket.priority not in f.priorities:
        return False
    if f.statuses and ticket.status not in f.statuses:
        return False
    for name, wanted in f.custom_fields.items():
        if not (_norm([ticket.fields.get(name)]) & set(wanted)):
            return False
    return True
