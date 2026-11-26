"""Typed config schema. Defaults are the built-in layer (spec section 8.3).

Every external value is config. Secrets are SecretRef (env var name or file
path), never literal values. Unknown keys are errors (see validate.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class SecretRef:
    env: str = ""
    file: str = ""


@dataclass(frozen=True)
class PluginSelection:
    """`adapter` is an alias from plugins.aliases or a dotted path 'pkg.module:Class'.
    `settings` is validated by the adapter's own Settings dataclass."""

    adapter: str = ""
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginsConfig:
    aliases: dict[str, str] = field(default_factory=dict)
    ticket_system: PluginSelection = field(default_factory=PluginSelection)
    git_host: PluginSelection = field(default_factory=PluginSelection)
    mailbox: PluginSelection = field(default_factory=PluginSelection)
    model_provider: PluginSelection = field(default_factory=PluginSelection)
    sandbox: PluginSelection = field(default_factory=PluginSelection)
    access_guard: PluginSelection = field(default_factory=PluginSelection)
    notifier: PluginSelection = field(default_factory=PluginSelection)
    monitor: PluginSelection = field(default_factory=PluginSelection)
    accounting: PluginSelection = field(default_factory=PluginSelection)
    state_store: PluginSelection = field(default_factory=PluginSelection)
    clock: PluginSelection = field(default_factory=PluginSelection)
    id_source: PluginSelection = field(default_factory=PluginSelection)


@dataclass(frozen=True)
class WorkflowConfig:
    """Jira status and workflow mapping.

    statuses: logical status (factory.ports.types.LogicalStatus value) -> native status name.
    transitions: optional native transition name per logical status, for workflows
      where the target status alone is ambiguous.
    fields: logical field name -> native custom field id.
    link_types: neutral link kind -> native link type name.
    claim_from: logical statuses a ticket may be claimed from.
    """

    statuses: dict[str, str] = field(default_factory=dict)
    transitions: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    link_types: dict[str, str] = field(default_factory=dict)
    claim_from: list[str] = field(default_factory=lambda: ["ready"])


@dataclass(frozen=True)
class LabelsConfig:
    ready: str = ""
    failed: str = ""
    needs_info: str = ""
    stalled: str = ""
    spec_approved: str = ""


@dataclass(frozen=True)
class CommenterRule:
    users: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    project_roles: list[str] = field(default_factory=list)
    include_reporter: bool = False
    include_assignee: bool = False


@dataclass(frozen=True)
class CollaborationConfig:
    """Per-channel rules, when set, replace the shared rule for that channel."""

    allowed_commenters: CommenterRule = field(default_factory=CommenterRule)
    jira_allowed_commenters: CommenterRule | None = None
    mr_allowed_commenters: CommenterRule | None = None
    bot_identities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OutputConfig:
    mode: Literal["merge_request", "branch_only"] = "merge_request"
    branch_pattern: str = "ai/{agent}/{ticket_key}"


@dataclass(frozen=True)
class JiraCommentsConfig:
    post: bool = True
    read_replies: bool = True
    verbosity: Literal["minimal", "milestones", "verbose"] = "milestones"
    status_comment_mode: Literal["append", "update_in_place"] = "update_in_place"
    poll_interval_seconds: int = 300
    stall_timeout_hours: int = 72
    max_question_rounds: int = 3
    comment_rate_limit_per_hour: int = 20
    needs_info_action: Literal["label", "status"] = "label"
    lease_while_paused: Literal["hold", "release"] = "release"


@dataclass(frozen=True)
class MrConfig:
    read_comments: bool = True
    respond: bool = True
    auto_fix: bool = True
    resolve_threads: bool = False
    poll_interval_seconds: int = 300
    inactivity_timeout_days: int = 14
    max_fix_rounds: int = 3
    apply_keyword: str = "/apply"
    template_path: str = ""


@dataclass(frozen=True)
class LimitsConfig:
    lease_minutes: int = 120
    max_attempts: int = 2
    max_tokens_per_ticket: int = 2_000_000
    max_files_changed: int = 15
    max_diff_lines: int = 600
    max_denied_access_attempts: int = 5


@dataclass(frozen=True)
class GitCloneConfig:
    depth: int = 1
    timeout_seconds: int = 600


@dataclass(frozen=True)
class GitSubmodulesConfig:
    enabled: bool = True
    recursive: bool = True
    depth: int = 1
    jobs: int = 4
    on_failure: Literal["fail", "continue_without"] = "fail"
    allow_modifications: bool = False


@dataclass(frozen=True)
class UrlRewrite:
    match: str = ""
    replace: str = ""


@dataclass(frozen=True)
class HostCredential:
    username: SecretRef = field(default_factory=SecretRef)
    token: SecretRef = field(default_factory=SecretRef)


@dataclass(frozen=True)
class GitConfig:
    executable: str = "git"
    clone: GitCloneConfig = field(default_factory=GitCloneConfig)
    submodules: GitSubmodulesConfig = field(default_factory=GitSubmodulesConfig)
    url_rewrites: list[UrlRewrite] = field(default_factory=list)
    host_credentials: dict[str, HostCredential] = field(default_factory=dict)


@dataclass(frozen=True)
class S3Tls:
    verify: bool = True
    ca_bundle_path: str = ""


@dataclass(frozen=True)
class S3Encryption:
    mode: Literal["", "sse-kms", "sse-s3"] = "sse-kms"
    kms_key_id: str = ""


@dataclass(frozen=True)
class S3Credentials:
    access_key_id: SecretRef = field(default_factory=SecretRef)
    secret_access_key: SecretRef = field(default_factory=SecretRef)
    session_token: SecretRef = field(default_factory=SecretRef)


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str = ""
    region: str = ""
    bucket: str = ""
    prefix: str = "factory-traces/"
    addressing: Literal["path", "virtual"] = "path"
    key_template: str = "{prefix}{agent}/{yyyy}/{mm}/{dd}/{ticket_key}/{run_id}/"
    tls: S3Tls = field(default_factory=S3Tls)
    encryption: S3Encryption = field(default_factory=S3Encryption)
    write_credentials: S3Credentials = field(default_factory=S3Credentials)
    read_credentials: S3Credentials = field(default_factory=S3Credentials)


@dataclass(frozen=True)
class LocalFsTraceConfig:
    root: str = "var/traces"
    retry_queue: str = "var/trace-retry"


@dataclass(frozen=True)
class RedactionConfig:
    extra_patterns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TraceStoreConfig:
    backend: Literal["memory", "local_fs", "s3"] = "local_fs"
    capture_level: Literal["metadata", "full"] = "full"
    require_encryption: bool = True
    flush_interval_seconds: int = 60
    local_fs: LocalFsTraceConfig = field(default_factory=LocalFsTraceConfig)
    s3: S3Config = field(default_factory=S3Config)
    redaction: RedactionConfig = field(default_factory=RedactionConfig)


@dataclass(frozen=True)
class AccessPolicyConfig:
    """`enabled` is only honoured from the global base config file."""

    enabled: bool = True
    deny_patterns: list[str] = field(default_factory=lambda: ["proprietary"])
    match_on: list[Literal["path", "content"]] = field(default_factory=lambda: ["path", "content"])
    case_insensitive: bool = True
    content_scan_bytes: int = 8192
    mode: Literal["strip", "deny_read"] = "strip"


@dataclass(frozen=True)
class EmailIntakeConfig:
    allow_updates: bool = False
    auto_label: bool = False
    allowlisted_senders: list[str] = field(default_factory=list)
    default_project: str = ""
    default_issue_type: str = ""


@dataclass(frozen=True)
class GitlabIntakeConfig:
    keyword: str = "/followup"
    default_project: str = ""
    default_issue_type: str = ""


@dataclass(frozen=True)
class RoutingRule:
    """First matching rule wins. Empty match fields are wildcards."""

    agent: str = ""
    repos: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoutingConfig:
    enabled: bool = False
    rules: list[RoutingRule] = field(default_factory=list)


@dataclass(frozen=True)
class IntakeConfig:
    email: EmailIntakeConfig = field(default_factory=EmailIntakeConfig)
    gitlab: GitlabIntakeConfig = field(default_factory=GitlabIntakeConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    dedupe_window_days: int = 90


@dataclass(frozen=True)
class StageModels:
    triage: str = ""
    spec: str = ""
    implement: str = ""
    review: str = ""
    intake: str = ""
    grading: str = ""


@dataclass(frozen=True)
class BudgetsConfig:
    user_tokens_per_day: int = 0
    team_tokens_per_day: int = 0
    agent_tokens_per_day: int = 0
    requests_per_minute: int = 0


@dataclass(frozen=True)
class ModelsConfig:
    stages: StageModels = field(default_factory=StageModels)
    budgets: BudgetsConfig = field(default_factory=BudgetsConfig)


@dataclass(frozen=True)
class GatesConfig:
    max_comment_density: float = 0.15
    min_changed_line_coverage: float = 0.8
    min_mutation_score: float = 0.6
    protected_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GlobalRulesConfig:
    """The pinned global factory repo: stages, checklist, PR templates, base skills."""

    path: str = "factory/skills"
    version: str = ""


@dataclass(frozen=True)
class FactoryConfig:
    profile: str = "sim"
    global_rules: GlobalRulesConfig = field(default_factory=GlobalRulesConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    labels: LabelsConfig = field(default_factory=LabelsConfig)
    collaboration: CollaborationConfig = field(default_factory=CollaborationConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    jira_comments: JiraCommentsConfig = field(default_factory=JiraCommentsConfig)
    mr: MrConfig = field(default_factory=MrConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    git: GitConfig = field(default_factory=GitConfig)
    trace_store: TraceStoreConfig = field(default_factory=TraceStoreConfig)
    access_policy: AccessPolicyConfig = field(default_factory=AccessPolicyConfig)
    intake: IntakeConfig = field(default_factory=IntakeConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    gates: GatesConfig = field(default_factory=GatesConfig)
    features: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class FilterConfig:
    labels_any: list[str] = field(default_factory=list)
    labels_all: list[str] = field(default_factory=list)
    labels_none: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    issue_types: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    custom_fields: dict[str, list[str]] = field(default_factory=dict)
    native_adapter: str = ""
    native_query: str = ""


@dataclass(frozen=True)
class ClaimConfig:
    order: list[str] = field(default_factory=lambda: ["priority", "created"])
    max_in_flight: int = 1


@dataclass(frozen=True)
class AgentOverrides:
    """Sections an agent may override. Anything not listed here, including
    access_policy.enabled, cannot be overridden per agent."""

    labels: dict[str, str] = field(default_factory=dict)
    output: dict[str, object] = field(default_factory=dict)
    jira_comments: dict[str, object] = field(default_factory=dict)
    mr: dict[str, object] = field(default_factory=dict)
    limits: dict[str, object] = field(default_factory=dict)
    models: dict[str, object] = field(default_factory=dict)
    features: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentConfig:
    name: str = ""
    enabled: bool = True
    service_account: str = ""
    team: str = ""
    repos: list[str] = field(default_factory=list)
    filter: FilterConfig = field(default_factory=FilterConfig)
    claim: ClaimConfig = field(default_factory=ClaimConfig)
    extra_deny_patterns: list[str] = field(default_factory=list)
    overrides: AgentOverrides = field(default_factory=AgentOverrides)
