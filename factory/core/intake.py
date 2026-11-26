"""One intake service for every source. Sources are adapters that produce
IntakeEvents; this service applies idempotency, dedupe, the shared readiness
checklist, trust, and routing. Bodies are untrusted and never grant authority.

Draft-only mode (a source's auto_label off): the factory proposes a readiness
verdict and a human applies the label. Every verdict is recorded, and
reconcile() later records each human override (a label applied against a
not-ready verdict, or a factory-applied label removed) and each confirmation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from factory.config.loader import LoadedConfig
from factory.core import readiness
from factory.core.stateindex import Index, Records
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
    auto_label: bool = True
    fields: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class IntakeResult:
    ticket_key: str
    created: bool
    duplicate_of: str = ""
    routed_to: str = ""
    ready: bool = False
    missing: tuple[str, ...] = ()
    verdict: str = ""
    label: str = ""


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
        self.verdicts = Records(state, "intake-verdicts")
        self.verdict_index = Index(state, "intake-verdict-index", "intake")
        self.overrides = Records(state, "intake-overrides")

    def ticket_url(self, key: str) -> str:
        template = self.cfg.workflow.ticket_url_template
        return template.format(key=key) if template else key

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
        verdict = "ready" if result.ready else "not_ready"
        labeled = result.ready and event.trusted and event.auto_label
        if labeled:
            self.writes.set_labels(ticket.key, add=[label])
            self.writes.transition(ticket.key, LogicalStatus.READY)
            self.writes.comment(ticket.key, "claimed", Note("Ready", "ready", done=f"Marked ready ({label})."),
                                f"intake-ready:{ticket.key}")
        elif result.ready:
            why = "the sender is not trusted (not allowlisted, or not authenticated)" if not event.trusted else "draft-only mode is on"
            self.writes.comment(ticket.key, "bounced", Note(
                "Created: proposed ready", "awaiting a human",
                done=f"The readiness check passed, but {why}, so a human confirms.",
                next=f"If you agree, apply the {label} label and move it to the ready status."),
                f"intake-proposed:{ticket.key}")
        else:
            self.writes.comment(ticket.key, "bounced", Note(
                "Created, not yet ready", "awaiting a human", done="Ticket created by the factory.",
                questions=result.failures, next="Add the missing information as a Jira comment."),
                f"intake-missing:{ticket.key}")
        self.verdicts.put(ticket.key, {"verdict": verdict, "label": label, "labeled_by_factory": labeled,
                                       "source": event.source, "at": self.clock.now().isoformat()})
        self.verdict_index.add(ticket.key)
        return IntakeResult(ticket.key, True, routed_to=agent, ready=labeled, missing=result.failures,
                            verdict=verdict, label=label)

    def _count(self, name: str, source: str) -> None:
        while True:
            raw = self.state.get("intake-stats", "all")
            stats = json.loads(raw or "{}")
            stats.setdefault(source, {"overrides": 0, "confirmations": 0})[name] += 1
            if self.state.compare_and_set("intake-stats", "all", raw, json.dumps(stats, sort_keys=True)):
                return

    def reconcile(self, metrics) -> list[dict]:
        """Record human overrides of intake verdicts. Returns the overrides found this pass."""
        found = []
        window = timedelta(days=self.cfg.intake.override_window_days)
        for key in self.verdict_index.members():
            v = self.verdicts.get(key)
            if v is None:
                self.verdict_index.remove(key)
                continue
            ticket = self.tickets.get(key)
            has_label = v["label"] in ticket.labels
            outcome = None
            if v["labeled_by_factory"] and not has_label:
                outcome = "override:label_removed"
            elif not v["labeled_by_factory"] and has_label:
                outcome = "confirmed" if v["verdict"] == "ready" else "override:marked_ready_against_verdict"
            if outcome is None:
                if self.clock.now() - datetime.fromisoformat(v["at"]) >= window:
                    self.verdicts.delete(key)
                    self.verdict_index.remove(key)
                continue
            record = {"ticket": key, "source": v["source"], "verdict": v["verdict"], "outcome": outcome,
                      "at": self.clock.now().isoformat()}
            self.overrides.put(key, record)
            self._count("overrides" if outcome.startswith("override") else "confirmations", v["source"])
            metrics.incr("intake_overrides" if outcome.startswith("override") else "intake_confirmations",
                         source=v["source"])
            self.verdicts.delete(key)
            self.verdict_index.remove(key)
            found.append(record)
        return found
