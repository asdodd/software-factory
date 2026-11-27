"""Deterministic claim with a lease and a lock label. Ticket systems have no
atomic claim, so after writing the claim the ticket is re-read and confirmed:

- the lease (lease_owner, lease_expires) settles races between workers of one
  agent, and lets a crashed run's ticket be reclaimed when the lease expires;
- the lock label (labels.claimed, one per agent through {agent}) settles races
  between agents whose filters overlap. Label edits are per label, so both
  agents' labels survive; on re-read the agent whose name sorts first keeps the
  ticket and the other backs off.

Claiming never changes the ticket's status. The worker moves it to in progress
when implementation starts; every other state is a label."""

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
                 metrics, dry_run: bool, other_locks: dict[str, str] | None = None) -> None:
        """other_locks: lock label -> agent name, for every other agent."""
        self.tickets = tickets
        self.writes = writes
        self.clock = clock
        self.agent = agent
        self.cfg = cfg
        self.worker_id = worker_id
        self.metrics = metrics
        self.dry_run = dry_run
        self.filter = to_filter(agent.filter)
        self.lock = cfg.labels.claimed
        self.other_locks = {label: name for label, name in (other_locks or {}).items() if label and label != self.lock}

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

    def _statuses(self) -> frozenset[str]:
        return self.filter.statuses or frozenset(self._native(LogicalStatus(s)) for s in self.cfg.workflow.claim_from)

    def ready_filter(self) -> TicketFilter:
        """The agent's filter, from the claimable statuses, without tickets that
        carry a state label or any agent's lock."""
        busy = {*self.cfg.labels.state_labels(), *self.other_locks, *([self.lock] if self.lock else [])}
        return dataclasses.replace(self.filter, statuses=self._statuses(), labels_none=self.filter.labels_none | busy)

    def candidates(self) -> list[tuple[Ticket, str]]:
        """(ticket, mode) where mode is ready, reclaim, or resume."""
        now = self.clock.now()
        out: list[tuple[Ticket, str]] = []
        labels = self.cfg.labels
        statuses = self._statuses()
        for t in self._search_all(self.ready_filter()):
            if not _leased(t, now, self.worker_id):
                out.append((t, "ready"))
        # Reclaim: this agent's tickets whose lease expired (a crashed run).
        # Mine = my lock label, or in progress with a lease one of my workers wrote.
        searches = [dataclasses.replace(self.filter, statuses=frozenset({self._native(LogicalStatus.IN_PROGRESS)}))]
        if self.lock:
            searches.append(dataclasses.replace(self.filter, statuses=frozenset(),
                                                labels_all=self.filter.labels_all | {self.lock}))
        for f in searches:
            for t in self._search_all(f):
                expiry = lease_expiry(t)
                if expiry is not None and expiry <= now and self._mine(t):
                    out.append((t, "reclaim"))
        if labels.spec_approved and labels.spec_review:
            waiting = frozenset(labels.state_labels()) - {labels.spec_review}
            approved = dataclasses.replace(
                self.filter, statuses=statuses,
                labels_all=self.filter.labels_all | {labels.spec_review, labels.spec_approved},
                labels_none=self.filter.labels_none | waiting | frozenset(self.other_locks))
            out.extend((t, "resume") for t in self._search_all(approved) if not _leased(t, now, self.worker_id))
        seen: dict[str, str] = {}
        for t, m in out:
            seen.setdefault(t.key, m)
        ranked = order({t.key: t for t, _ in out}.values(), self.agent.claim.order, self.cfg.workflow.priority_order)
        return [(t, seen[t.key]) for t in ranked]

    def _mine(self, ticket: Ticket) -> bool:
        owner = str(ticket.fields.get("lease_owner") or "")
        return self.lock in ticket.labels or owner.startswith(f"{self.agent.name}:")

    def held_by_other_agent(self, ticket: Ticket) -> list[str]:
        return sorted(self.other_locks[label] for label in ticket.labels if label in self.other_locks)

    def claim(self, key: str, mode: str) -> Ticket | None:
        fresh = self.tickets.get(key)
        now = self.clock.now()
        if mode == "ready":
            check = self.ready_filter()
        else:
            check = dataclasses.replace(self.filter, statuses=frozenset({fresh.status}))
        if not matches(check, fresh):
            self.metrics.incr("claim_skipped", reason="filter_changed")
            return None
        if self.held_by_other_agent(fresh):
            self.metrics.incr("claim_skipped", reason="locked_by_other_agent")
            return None
        if mode == "reclaim":
            expiry = lease_expiry(fresh)
            if expiry is None or expiry > now or not self._mine(fresh):
                return None
        elif _leased(fresh, now, self.worker_id):
            return None
        self.writes.set_labels(key, add=[self.lock])
        if not self.dry_run:
            locked = self.tickets.get(key)
            if self.held_by_other_agent(locked):
                # Another agent added its lock at the same time. Whoever sees a
                # rival backs off before writing a lease, so at most one wins
                # (both may back off; the next cycle tries again).
                self.metrics.incr("claim_lost", reason="other_agent")
                self.writes.set_labels(key, remove=[self.lock])
                return None
        expires = now + timedelta(minutes=self.cfg.limits.lease_minutes)
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
        """Drop the lease and the lock label."""
        self.writes.update(key, {"lease_owner": "", "lease_expires": ""})
        self.writes.set_labels(key, remove=[self.lock])


def _leased(ticket: Ticket, now: datetime, me: str) -> bool:
    owner = ticket.fields.get("lease_owner")
    expiry = lease_expiry(ticket)
    return bool(owner) and owner != me and expiry is not None and expiry > now
