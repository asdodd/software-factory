"""Value types shared by every port.

Adapters and core may both import this module. Nothing here performs I/O.
All native names (statuses, labels, field ids) are plain strings resolved
from config by core; this module never names a concrete Jira or GitLab value.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")

TicketKey = str
Cursor = str


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: Cursor | None


class PrincipalKind(enum.Enum):
    USER = "user"
    SERVICE = "service"
    BOT = "bot"


@dataclass(frozen=True)
class Principal:
    id: str
    display_name: str = ""
    kind: PrincipalKind = PrincipalKind.USER
    groups: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()


class LogicalStatus(enum.Enum):
    """The statuses the factory knows. Humans own the workflow: the factory
    claims from READY and may move a ticket to IN_PROGRESS, nothing else.
    Every other state is a label (see LabelsConfig)."""

    READY = "ready"
    IN_PROGRESS = "in_progress"


class LinkKind(enum.Enum):
    """Neutral link kinds. Config maps each to a native link type name."""

    RELATES = "relates"
    BLOCKS = "blocks"
    DUPLICATES = "duplicates"


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str
    media_type: str
    size: int


@dataclass(frozen=True)
class Ticket:
    key: TicketKey
    project: str
    summary: str
    description: str
    status: str
    issue_type: str
    priority: str
    labels: frozenset[str]
    components: frozenset[str]
    reporter: Principal | None
    assignee: Principal | None
    created: datetime
    updated: datetime
    fields: Mapping[str, object] = field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class TicketDraft:
    project: str
    summary: str
    description: str
    issue_type: str
    reporter: Principal | None
    labels: frozenset[str] = frozenset()
    components: frozenset[str] = frozenset()
    fields: Mapping[str, object] = field(default_factory=dict)
    idempotency_key: str = ""


@dataclass(frozen=True)
class TicketUpdate:
    """Partial update. None means unchanged. Field keys are logical names."""

    summary: str | None = None
    description: str | None = None
    assignee: Principal | None = None
    fields: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Comment:
    id: str
    author: Principal
    body: str
    created: datetime
    updated: datetime | None = None


@dataclass(frozen=True)
class NativeQuery:
    """Adapter-specific escape hatch. Rejected by any other adapter."""

    adapter: str
    query: str


@dataclass(frozen=True)
class TicketFilter:
    """Ticket-system-neutral filter. Empty dimension means unconstrained.

    Custom fields are keyed by logical field name from config; values match any-of.
    """

    labels_any: frozenset[str] = frozenset()
    labels_all: frozenset[str] = frozenset()
    labels_none: frozenset[str] = frozenset()
    projects: frozenset[str] = frozenset()
    components: frozenset[str] = frozenset()
    issue_types: frozenset[str] = frozenset()
    priorities: frozenset[str] = frozenset()
    statuses: frozenset[str] = frozenset()
    custom_fields: Mapping[str, frozenset[str]] = field(default_factory=dict)
    native: NativeQuery | None = None


@dataclass(frozen=True)
class RepoRef:
    host: str
    path: str
    default_branch: str


@dataclass(frozen=True)
class SubmoduleState:
    path: str
    url: str
    recorded_commit: str
    checked_out_commit: str | None
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class CheckoutResult:
    workspace: Path
    head_commit: str
    submodules: tuple[SubmoduleState, ...]


class MergeRequestState(enum.Enum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


@dataclass(frozen=True)
class MergeRequestDraft:
    repo: RepoRef
    source_branch: str
    target_branch: str
    title: str
    description: str
    idempotency_key: str


@dataclass(frozen=True)
class MergeRequest:
    repo: RepoRef
    id: str
    url: str
    source_branch: str
    target_branch: str
    state: MergeRequestState


@dataclass(frozen=True)
class MrComment:
    id: str
    thread_id: str
    author: Principal
    body: str
    created: datetime
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class Suggestion:
    path: str
    line_start: int
    line_end: int
    replacement: str
    note: str


@dataclass(frozen=True)
class HostCommentEvent:
    """A comment anywhere on the git host, used by GitLab-comment intake."""

    event_id: str
    repo: RepoRef
    target_kind: str
    target_id: str
    author: Principal
    body: str
    created: datetime
    url: str


@dataclass(frozen=True)
class MailAttachment:
    filename: str
    media_type: str
    content: bytes


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    thread_id: str
    in_reply_to: str | None
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    headers: Mapping[str, str]
    received: datetime
    attachments: tuple[MailAttachment, ...] = ()


@dataclass(frozen=True)
class MailThread:
    thread_id: str
    messages: tuple[MailMessage, ...]


@dataclass(frozen=True)
class MailReply:
    thread_id: str
    in_reply_to: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    idempotency_key: str


class Role(enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    json_schema: Mapping[str, object]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class CallContext:
    """Who a model call is for. Drives budgets and attribution."""

    agent: str
    ticket_key: TicketKey
    stage: str
    attributed_to: str
    team: str


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[Message, ...]
    context: CallContext
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int = 4096
    temperature: float = 0.0


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelResponse:
    model: str
    message: Message
    usage: Usage
    finish_reason: str


@dataclass(frozen=True)
class WorkspaceSpec:
    run_id: str
    agent: str
    ticket_key: TicketKey


@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: str = "."
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 600.0
    stdin_path: str = ""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


@dataclass(frozen=True)
class Artifact:
    name: str
    path: Path
    media_type: str


class NotificationKind(enum.Enum):
    SPEC_REVIEW = "spec_review"
    MR_READY = "mr_ready"
    TICKET_FAILED = "ticket_failed"
    TICKET_BLOCKED = "ticket_blocked"


@dataclass(frozen=True)
class Notification:
    kind: NotificationKind
    ticket_key: TicketKey
    recipients: tuple[str, ...]
    subject: str
    body: str
    link: str = ""


@dataclass(frozen=True)
class Finding:
    event_id: str
    source: str
    repo: str
    location: str
    summary: str
    details: str
    severity: str


class Outcome(enum.Enum):
    MR_OPENED = "mr_opened"
    SPEC_REVIEW = "spec_review"
    CLAIM_LOST = "claim_lost"
    RESUMED = "resumed"
    FIX_PUSHED = "fix_pushed"
    FIX_FAILED = "fix_failed"
    REPLIED = "replied"
    BRANCH_PUSHED = "branch_pushed"
    NEEDS_INFO = "needs_info"
    BLOCKED = "blocked"
    STALLED = "stalled"
    FAILED = "failed"
    BOUNCED = "bounced"


@dataclass(frozen=True)
class RunMeta:
    schema_version: int
    run_id: str
    agent: str
    ticket_key: TicketKey
    repo: str
    models: Mapping[str, str]
    factory_version: str
    config_hash: str
    started: datetime
    finished: datetime | None
    stage_seconds: Mapping[str, float]
    usage: Usage
    outcome: Outcome | None


@dataclass(frozen=True)
class TraceEvent:
    at: datetime
    kind: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class OutcomeRecord:
    """Appended after a run as a new immutable object, never an overwrite."""

    run_id: str
    at: datetime
    kind: str
    payload: Mapping[str, object]
    agent: str = ""
    ticket_key: TicketKey = ""
    run_started: datetime | None = None


@dataclass(frozen=True)
class TraceQuery:
    agent: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    ticket_key: TicketKey | None = None
    outcome: Outcome | None = None


class SpendScopeKind(enum.Enum):
    TICKET = "ticket"
    USER = "user"
    TEAM = "team"
    AGENT = "agent"


@dataclass(frozen=True)
class SpendScope:
    kind: SpendScopeKind
    id: str


@dataclass(frozen=True)
class SpendRecord:
    at: datetime
    context: CallContext
    model: str
    usage: Usage


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    scope: SpendScope | None = None
    spent_tokens: int = 0
    limit_tokens: int | None = None


@dataclass(frozen=True)
class ModelEndpoint:
    """What a harness is given to reach a model: the factory's loopback
    gateway, never the upstream provider or its credentials."""

    base_url: str
    token: str
    model: str
    dialects: tuple[str, ...]


class HarnessStatus(enum.Enum):
    DONE = "done"
    NEEDS_INFO = "needs_info"
    BLOCKED = "blocked"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class HarnessTask:
    """One stage of work handed to a harness.

    workspace is an isolated copy with git history replaced by a single
    baseline commit, and with the agent kit installed (the restricted-files
    skill and the hooks that back it). With several repos, workspace.root
    holds one directory per repo. The factory, not the harness, diffs the
    result and carries it to the real branches.
    instructions_file holds the rendered prompt: ticket, approved spec, base
    skills, global rules, and repo rules. result_file is where the harness may
    write a HarnessResult as JSON.
    """

    run_id: str
    stage: str
    ticket_key: TicketKey
    workspace: Workspace
    instructions_file: Path
    result_file: Path
    endpoint: ModelEndpoint
    timeout_seconds: float
    max_turns: int


@dataclass(frozen=True)
class HarnessResult:
    status: HarnessStatus
    summary: str
    questions: tuple[str, ...] = ()
    events: tuple[TraceEvent, ...] = ()
