"""One intake service for every source. Sources are adapters that produce
IntakeEvents; this service applies idempotency, dedupe, the shared readiness
checklist, trust, and routing. Bodies are untrusted and never grant authority."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from factory.config.loader import LoadedConfig
from factory.core import readiness
from factory.core.writes import Note, Writes
from factory.ports.types import LogicalStatus, Principal, Ticket, TicketDraft, TicketFilter

_WS = re.compile(r"\s+")
_NOISE = re.compile(r"[^a-z0-9 ]+")


@dataclass(frozen=True)
class IntakeEvent:
    source: str
    event_id: str
    project: str
    summary: str
    description: str = ""
    issue_type: str = "Task"
    repo: str = ""
    location: str = ""
    components: tuple[str, ...] = ()
    reporter: str = ""
    trusted: bool = False
    fields: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class IntakeResult:
    ticket_key: str
    created: bool
    duplicate_of: str = ""
    routed_to: str = ""
    ready: bool = False
    missing: tuple[str, ...] = ()


def fingerprint(repo: str, location: str, summary: str) -> str:
    norm = _WS.sub(" ", _NOISE.sub(" ", summary.lower())).strip()
    return hashlib.sha256(f"{repo}|{location}|{norm}".encode()).hexdigest()[:20]


class IntakeService:
    def __init__(self, loaded: LoadedConfig, tickets, state, clock, writes: Writes) -> None:
        self.loaded = loaded
        self.cfg = loaded.config
        self.tickets = tickets
        self.state = state
        self.clock = clock
        self.writes = writes

    def route(self, repo: str, components: tuple[str, ...], project: str) -> str:
        if not self.loaded.features.on("intake.routing"):
            return ""
        for rule in self.cfg.intake.routing.rules:
            if rule.repos and repo not in rule.repos:
                continue
            if rule.components and not set(rule.components) & set(components):
                continue
            if rule.projects and project not in rule.projects:
                continue
            return rule.agent
        return ""

    def submit(self, event: IntakeEvent) -> IntakeResult:
        idem = f"{event.source}:{event.event_id}"
        seen = self.state.get("intake-events", idem)
        if seen:
            return IntakeResult(seen, False)
        fp = fingerprint(event.repo, event.location, event.summary)
        existing = self.tickets.search(TicketFilter(custom_fields={"fingerprint": frozenset({fp})})).items
        if existing:
            dup = existing[0]
            self.writes.comment(dup.key, "finished", Note("Related report", "unchanged",
                                done=f"A matching report arrived from {event.source}; no duplicate was created."),
                                f"intake:{idem}")
            self.state.put("intake-events", idem, dup.key)
            return IntakeResult(dup.key, False, duplicate_of=dup.key)
        fields = {**dict(event.fields), "fingerprint": fp, "repo": event.repo, "source_ref": idem}
        draft = TicketDraft(event.project, event.summary, event.description, event.issue_type,
                            Principal(event.reporter, event.reporter) if event.reporter else None,
                            components=frozenset(event.components), fields=fields, idempotency_key=idem)
        ticket = self.writes.create(draft)
        if ticket is None:
            return IntakeResult("", False)
        self.state.put("intake-events", idem, ticket.key)
        return self._after_create(ticket, event)

    def _after_create(self, ticket: Ticket, event: IntakeEvent) -> IntakeResult:
        agent = self.route(event.repo, event.components, event.project)
        scope = self.loaded.agents[agent].repos if agent else None
        result = readiness.evaluate(ticket, self.cfg.limits, scope)
        label = self.loaded.agent_configs[agent].labels.ready if agent else self.cfg.labels.ready
        if result.ready and event.trusted:
            self.writes.set_labels(ticket.key, add=[label])
            self.writes.transition(ticket.key, LogicalStatus.READY)
            self.writes.comment(ticket.key, "claimed", Note("Ready", "ready", done=f"Marked ready ({label})."),
                                f"intake-ready:{ticket.key}")
        else:
            why = result.failures or ("A human must confirm readiness and apply the label.",)
            self.writes.comment(ticket.key, "bounced", Note("Created, not yet ready", "awaiting a human",
                                done="Ticket created by the factory.", questions=tuple(why),
                                next="Add the missing information as a Jira comment."), f"intake-missing:{ticket.key}")
        return IntakeResult(ticket.key, True, routed_to=agent, ready=result.ready and event.trusted,
                            missing=result.failures)
