"""Deterministic claim with a lease. Ticket systems have no atomic claim, so
after writing the claim the ticket is re-read and the lease owner confirmed."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from datetime import datetime, timedelta

from factory.config.schema import AgentConfig, FactoryConfig, FilterConfig
from factory.core.writes import Writes
from factory.ports.filter_semantics import matches
from factory.ports.types import LogicalStatus, NativeQuery, Ticket, TicketFilter


def to_filter(f: FilterConfig) -> TicketFilter:
    return TicketFilter(
        labels_any=frozenset(f.labels_any), labels_all=frozenset(f.labels_all), labels_none=frozenset(f.labels_none),
        projects=frozenset(f.projects), components=frozenset(f.components), issue_types=frozenset(f.issue_types),
        priorities=frozenset(f.priorities), statuses=frozenset(f.statuses),
        custom_fields={k: frozenset(v) for k, v in f.custom_fields.items()},
        native=NativeQuery(f.native_adapter, f.native_query) if f.native_query else None,
    )


def lease_expiry(ticket: Ticket) -> datetime | None:
    raw = ticket.fields.get("lease_expires")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def order(tickets: Iterable[Ticket], keys: list[str], priority_order: list[str]) -> list[Ticket]:
    rank = {p: i for i, p in enumerate(priority_order)}
    result = list(tickets)
    for key in reversed(keys):
        reverse = key.startswith("-")
        name = key.lstrip("-")
        if name == "priority":
            result.sort(key=lambda t: rank.get(t.priority, len(rank)), reverse=reverse)
        elif name == "created":
            result.sort(key=lambda t: t.created, reverse=reverse)
        elif name == "key":
            result.sort(key=lambda t: t.key, reverse=reverse)
    return result


class Claimer:
    def __init__(self, tickets, writes: Writes, clock, agent: AgentConfig, cfg: FactoryConfig, worker_id: str,
                 metrics, dry_run: bool) -> None:
        self.tickets = tickets
        self.writes = writes
        self.clock = clock
        self.agent = agent
        self.cfg = cfg
        self.worker_id = worker_id
        self.metrics = metrics
        self.dry_run = dry_run
        self.filter = to_filter(agent.filter)

    def _search_all(self, f: TicketFilter) -> list[Ticket]:
        found, cursor = [], None
        while True:
            page = self.tickets.search(f, cursor)
            found.extend(page.items)
            if not page.next_cursor:
                return found
            cursor = page.next_cursor

    def _native(self, status: LogicalStatus) -> str:
        return self.writes.native_status(status)

    def candidates(self) -> list[tuple[Ticket, str]]:
        """(ticket, mode) where mode is ready, reclaim, or resume."""
        now = self.clock.now()
        out: list[tuple[Ticket, str]] = []
        claim_from = {self._native(LogicalStatus(s)) for s in self.cfg.workflow.claim_from}
        ready_filter = self.filter if self.filter.statuses else dataclasses.replace(self.filter, statuses=frozenset(claim_from))
        for t in self._search_all(ready_filter):
            if not _leased(t, now, self.worker_id):
                out.append((t, "ready"))
        stale = dataclasses.replace(self.filter, statuses=frozenset({self._native(LogicalStatus.IN_PROGRESS)}))
        for t in self._search_all(stale):
            expiry = lease_expiry(t)
            if expiry is not None and expiry <= now:
                out.append((t, "reclaim"))
        if self.cfg.labels.spec_approved:
            approved = dataclasses.replace(self.filter, statuses=frozenset({self._native(LogicalStatus.SPEC_REVIEW)}),
                                           labels_all=self.filter.labels_all | {self.cfg.labels.spec_approved})
            out.extend((t, "resume") for t in self._search_all(approved))
        ranked = order([t for t, _ in out], self.agent.claim.order, self.cfg.workflow.priority_order)
        modes = {t.key: m for t, m in out}
        return [(t, modes[t.key]) for t in ranked]

    def claim(self, key: str, mode: str) -> Ticket | None:
        fresh = self.tickets.get(key)
        now = self.clock.now()
        check = self.filter
        if mode != "ready":
            check = dataclasses.replace(self.filter, statuses=frozenset({fresh.status}))
        if not matches(check, fresh):
            self.metrics.incr("claim_skipped", reason="filter_changed")
            return None
        if mode == "reclaim":
            expiry = lease_expiry(fresh)
            if expiry is None or expiry > now:
                return None
        elif _leased(fresh, now, self.worker_id):
            return None
        expires = now + timedelta(minutes=self.cfg.limits.lease_minutes)
        self.writes.transition(key, LogicalStatus.IN_PROGRESS)
        self.writes.update(key, {"lease_owner": self.worker_id, "lease_expires": expires.isoformat()},
                           assignee=self.agent.service_account)
        if self.dry_run:
            return fresh
        confirmed = self.tickets.get(key)
        if confirmed.fields.get("lease_owner") != self.worker_id:
            self.metrics.incr("claim_lost")
            return None
        return confirmed

    def release(self, key: str) -> None:
        self.writes.update(key, {"lease_owner": "", "lease_expires": ""})


def _leased(ticket: Ticket, now: datetime, me: str) -> bool:
    owner = ticket.fields.get("lease_owner")
    expiry = lease_expiry(ticket)
    return bool(owner) and owner != me and expiry is not None and expiry > now
