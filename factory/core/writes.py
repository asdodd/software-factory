"""The one library every external write goes through. The model never calls
it. Dry-run logs intended writes instead of performing them. Comments are
idempotent, rate limited, filtered by verbosity, and secrets-redacted."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from factory.config.schema import FactoryConfig
from factory.ports.errors import Conflict
from factory.ports.types import (
    LinkKind,
    LogicalStatus,
    MergeRequest,
    MergeRequestDraft,
    MergeRequestState,
    Principal,
    PrincipalKind,
    Ticket,
    TicketDraft,
    TicketKey,
    TicketUpdate,
)
from factory.redaction import Redactor

log = logging.getLogger("factory.writes")

MINIMAL = {"claimed", "spec_posted", "needs_info", "blocked", "failed", "bounced", "finished", "stalled"}
MILESTONES = MINIMAL | {"triaged", "implementation_started", "branch_pushed", "mr_opened", "review_done",
                        "verification_done", "reclaimed", "resumed", "mr_feedback", "email_update"}
STANDALONE = {"needs_info", "bounced", "blocked", "failed", "finished", "spec_posted", "stalled", "reclaimed",
              "resumed", "mr_feedback", "email_update"}


@dataclass
class Note:
    title: str
    status: str
    done: str = ""
    next: str = ""
    questions: tuple[str, ...] = ()
    links: Mapping[str, str] = field(default_factory=dict)
    details: str = ""

    def render(self, verbose: bool) -> str:
        lines = [f"[factory] {self.title}", f"Status: {self.status}"]
        if self.done:
            lines.append(f"Done: {self.done}")
        if self.next:
            lines.append(f"Next: {self.next}")
        for name, url in self.links.items():
            lines.append(f"{name}: {url}")
        if self.questions:
            lines.append("Open questions:")
            lines.extend(f"{i}. {q}" for i, q in enumerate(self.questions, 1))
        if verbose and self.details:
            lines += ["", self.details]
        return "\n".join(lines)


class Writes:
    def __init__(self, tickets, git, state, clock, redactor: Redactor, config: FactoryConfig, agent: str,
                 dry_run: bool) -> None:
        self.tickets = tickets
        self.git = git
        self.state = state
        self.clock = clock
        self.redactor = redactor
        self.config = config
        self.agent = agent
        self.dry_run = dry_run
        self.intended: list[tuple[str, object]] = []

    def _intend(self, op: str, detail: object) -> None:
        self.intended.append((op, detail))
        log.info("dry-run: would %s %s", op, self.redactor.redact(json.dumps(detail, default=str)).text)

    # ---- ticket writes ------------------------------------------------
    def native_status(self, status: LogicalStatus) -> str:
        native = self.config.workflow.statuses.get(status.value)
        if not native:
            raise Conflict(f"workflow.statuses.{status.value} is not configured")
        return native

    def transition(self, key: TicketKey, status: LogicalStatus) -> None:
        native = self.native_status(status)
        if self.dry_run:
            return self._intend("transition", {"ticket": key, "to": native})
        self.tickets.transition(key, native)

    def set_labels(self, key: TicketKey, add: Iterable[str] = (), remove: Iterable[str] = ()) -> None:
        add = [label for label in add if label]
        remove = [label for label in remove if label]
        if not add and not remove:
            return
        if self.dry_run:
            return self._intend("labels", {"ticket": key, "add": add, "remove": remove})
        self.tickets.set_labels(key, add, remove)

    def update(self, key: TicketKey, fields: Mapping[str, object] | None = None, assignee: str | None = None) -> None:
        update = TicketUpdate(
            assignee=Principal(assignee, assignee, PrincipalKind.SERVICE) if assignee is not None else None,
            fields=dict(fields or {}),
        )
        if self.dry_run:
            return self._intend("update", {"ticket": key, "fields": dict(fields or {}), "assignee": assignee})
        self.tickets.update(key, update)

    def link(self, source: TicketKey, target: TicketKey, kind: LinkKind) -> None:
        if self.dry_run:
            return self._intend("link", {"source": source, "target": target, "kind": kind.value})
        self.tickets.link(source, target, kind)

    def create(self, draft: TicketDraft) -> Ticket | None:
        if self.dry_run:
            self._intend("create", {"summary": draft.summary, "project": draft.project})
            return None
        return self.tickets.create(draft)

    # ---- comments -------------------------------------------------------
    def _rate_ok(self, key: TicketKey) -> bool:
        ns = f"comment-times:{self.agent}"
        now = self.clock.now()
        times = [t for t in json.loads(self.state.get(ns, key) or "[]")
                 if now - datetime.fromisoformat(t) < timedelta(hours=1)]
        if len(times) >= self.config.jira_comments.comment_rate_limit_per_hour:
            return False
        times.append(now.isoformat())
        self.state.put(ns, key, json.dumps(times))
        return True

    def comment(self, key: TicketKey, milestone: str, note: Note, idempotency_key: str) -> str | None:
        """Returns the id of the comment written or edited, or None when nothing was posted."""
        jc = self.config.jira_comments
        if not jc.post:
            return None
        allowed = MINIMAL if jc.verbosity == "minimal" else MILESTONES
        if jc.verbosity != "verbose" and milestone not in allowed:
            return None
        body = self.redactor.redact(note.render(jc.verbosity == "verbose")).text
        if self.dry_run:
            self._intend("comment", {"ticket": key, "milestone": milestone, "body": body})
            return None
        if not self._rate_ok(key):
            log.warning("comment rate limit reached on %s; skipping %s", key, milestone)
            return None
        living = self.state.get(f"status-comment:{self.agent}", key)
        if jc.status_comment_mode == "update_in_place" and milestone not in STANDALONE and living:
            return self.tickets.edit_comment(key, living, body).id
        posted = self.tickets.add_comment(key, body, idempotency_key)
        if jc.status_comment_mode == "update_in_place" and milestone not in STANDALONE:
            self.state.put(f"status-comment:{self.agent}", key, posted.id)
        return posted.id

    # ---- intake replies --------------------------------------------------------
    def mail_reply(self, mailbox, reply) -> None:
        reply = type(reply)(reply.thread_id, reply.in_reply_to, reply.to, reply.cc, reply.subject,
                            self.redactor.redact(reply.body).text, reply.idempotency_key)
        if self.dry_run:
            return self._intend("mail_reply", {"to": list(reply.to), "cc": list(reply.cc), "subject": reply.subject})
        mailbox.send_reply(reply)

    def host_comment(self, event, body: str, idempotency_key: str) -> None:
        body = self.redactor.redact(body).text
        if self.dry_run:
            return self._intend("host_comment", {"url": event.url, "body": body})
        self.git.comment_on(event, body, idempotency_key)

    # ---- merge request comments ----------------------------------------------
    def mr_reply(self, repo, mr_id: str, thread_id: str, body: str, idempotency_key: str) -> None:
        body = self.redactor.redact(body).text
        if self.dry_run:
            return self._intend("mr_reply", {"mr": mr_id, "thread": thread_id, "body": body})
        self.git.reply_in_thread(repo, mr_id, thread_id, body, idempotency_key)

    def mr_suggestion(self, repo, mr_id: str, suggestion, idempotency_key: str) -> None:
        if self.dry_run:
            return self._intend("mr_suggestion", {"mr": mr_id, "path": suggestion.path})
        self.git.post_suggestion(repo, mr_id, suggestion, idempotency_key)

    # ---- git host writes ------------------------------------------------
    def push_branch(self, workspace: Path, branch: str) -> str:
        prefix = self.config.output.branch_pattern.split("{ticket_key}")[0].format(agent=self.agent)
        if not branch.startswith(prefix):
            raise Conflict(f"refusing to push {branch}: agents push only their own branches ({prefix}*)")
        if self.dry_run:
            self._intend("push", {"branch": branch})
            return "dry-run"
        return self.git.push_branch(workspace, branch)

    def open_merge_request(self, draft: MergeRequestDraft) -> MergeRequest:
        if self.dry_run:
            self._intend("open_mr", {"repo": draft.repo.path, "branch": draft.source_branch})
            return MergeRequest(draft.repo, "dry-run", "dry-run://mr", draft.source_branch, draft.target_branch,
                                MergeRequestState.OPEN)
        return self.git.open_merge_request(draft)
