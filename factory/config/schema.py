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
    priority_order: list[str] = field(default_factory=list)
    ticket_url_template: str = ""


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
    factory_identity: str = ""


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
    max_ticket_chars: int = 12000
    max_acceptance_criteria: int = 12


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
    default_branch: str = "main"
    branches: dict[str, str] = field(default_factory=dict)
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
    """address: the bot's own address. Mail is read only when the bot is in To or Cc.
    allowlisted_senders may use shell-style wildcards (*@team.example).
    user_map maps a sender address to a ticket-system user id; unmapped senders
    become the reporter by address. Senders matching ignore_senders, auto-generated
    mail, and the bot's own mail are never answered (loop protection)."""

    address: str = ""
    allow_updates: bool = False
    auto_label: bool = False
    allowlisted_senders: list[str] = field(default_factory=list)
    require_authenticated_sender: bool = False
    user_map: dict[str, str] = field(default_factory=dict)
    ignore_senders: list[str] = field(default_factory=lambda: [
        "mailer-daemon@*", "postmaster@*", "noreply@*", "no-reply@*", "donotreply@*"])
    max_reply_recipients: int = 10
    max_attachment_bytes: int = 200_000
    default_project: str = ""
    default_issue_type: str = ""


@dataclass(frozen=True)
class GitlabIntakeConfig:
    """A comment whose first word is keyword creates a follow-up ticket.
    allowed_authors may get the ready label automatically when auto_label is on."""

    keyword: str = "/followup"
    auto_label: bool = False
    allowed_authors: list[str] = field(default_factory=list)
    reply: bool = True
    default_project: str = ""
    default_issue_type: str = ""


@dataclass(frozen=True)
class MonitoringIntakeConfig:
    """Findings from configured monitors are trusted sources; auto_label decides
    whether a ready finding is labeled without a human (draft-only by default)."""

    auto_label: bool = False
    default_project: str = ""
    default_issue_type: str = ""
    min_severity: Literal["info", "low", "medium", "high", "critical"] = "medium"


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
    monitoring: MonitoringIntakeConfig = field(default_factory=MonitoringIntakeConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    dedupe_window_days: int = 90
    override_window_days: int = 14


@dataclass(frozen=True)
class StageModel:
    """Which model serves a stage, and which harness drives it.

    provider: a key of models.providers (bring your own model).
    harness: a key of harnesses (bring your own harness); "" = single model
      calls with no tool loop, which is all triage, spec, and intake need.
    """

    provider: str = ""
    model: str = ""
    harness: str = ""


@dataclass(frozen=True)
class StageModels:
    triage: StageModel = field(default_factory=StageModel)
    spec: StageModel = field(default_factory=StageModel)
    implement: StageModel = field(default_factory=lambda: StageModel(harness="builtin"))
    review: StageModel = field(default_factory=StageModel)
    intake: StageModel = field(default_factory=StageModel)
    grading: StageModel = field(default_factory=StageModel)
    respond: StageModel = field(default_factory=StageModel)
    improve: StageModel = field(default_factory=StageModel)


@dataclass(frozen=True)
class BudgetsConfig:
    user_tokens_per_day: int = 0
    team_tokens_per_day: int = 0
    agent_tokens_per_day: int = 0
    requests_per_minute: int = 0


@dataclass(frozen=True)
class ModelsConfig:
    """providers: name -> ModelProvider plugin. Several may be configured at
    once (e.g. a government endpoint and a gateway); stages pick by name."""

    providers: dict[str, PluginSelection] = field(default_factory=dict)
    stages: StageModels = field(default_factory=StageModels)
    budgets: BudgetsConfig = field(default_factory=BudgetsConfig)


@dataclass(frozen=True)
class GatewayConfig:
    """Loopback model gateway handed to every harness. It is the harness's
    only route to a model, so budgets, accounting, redaction, and trace
    capture apply to bring-your-own harnesses too."""

    bind_host: str = "127.0.0.1"
    dialects: list[Literal["openai_chat", "anthropic_messages"]] = field(
        default_factory=lambda: ["openai_chat", "anthropic_messages"]
    )
    request_timeout_seconds: int = 300


@dataclass(frozen=True)
class HarnessConfig:
    """`adapter` is an alias or 'pkg.module:Class' implementing AgentHarness.
    `settings` is validated by the adapter (e.g. argv template for harness.command)."""

    adapter: str = ""
    timeout_minutes: int = 60
    max_turns: int = 200
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GradingConfig:
    """The grader is trusted only after calibration against human judgments
    reaches min_agreement (each score within tolerance of the human's)."""

    lookback_days: int = 7
    max_runs_per_job: int = 200
    min_agreement: float = 0.8
    tolerance: int = 1


@dataclass(frozen=True)
class SelfImprovementConfig:
    """Proposals are opened as MRs against the factory repo; nothing is applied
    automatically. Edits outside allowed_paths are dropped."""

    repo: str = ""
    base_branch: str = "main"
    allowed_paths: list[str] = field(default_factory=lambda: ["factory/skills/**", "config/agents/**"])
    max_files: int = 10
    lookback_days: int = 14


@dataclass(frozen=True)
class FeedbackConfig:
    grading: GradingConfig = field(default_factory=GradingConfig)
    self_improvement: SelfImprovementConfig = field(default_factory=SelfImprovementConfig)


@dataclass(frozen=True)
class SkillRef:
    """A stage skill by id ("<pack>/<name>"), optionally conditional on the
    ticket's issue type or on an approved spec existing."""

    skill: str = ""
    issue_types: list[str] = field(default_factory=list)
    requires_spec: bool = False


@dataclass(frozen=True)
class StageSkills:
    spec: list[SkillRef] = field(default_factory=list)
    implement: list[SkillRef] = field(default_factory=list)
    review: list[SkillRef] = field(default_factory=list)
    improve: list[SkillRef] = field(default_factory=list)


@dataclass(frozen=True)
class SkillsConfig:
    """Optional third-party skills per stage, rendered after the mandatory base
    skills (which always win). Vendored packs live in factory/skills/vendor;
    extra_paths adds your own packs laid out as <path>/<pack>/<name>/SKILL.md."""

    enabled: bool = True
    stages: StageSkills = field(default_factory=StageSkills)
    extra_paths: list[str] = field(default_factory=list)
    max_chars_per_stage: int = 60000
    install_native: bool = True


@dataclass(frozen=True)
class NotificationsConfig:
    """Who is alerted, by user id. The reporter is added when include_reporter is true."""

    include_reporter: bool = True
    spec_review: list[str] = field(default_factory=list)
    mr_ready: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GatesConfig:
    max_comment_density: float = 0.15
    min_changed_line_coverage: float = 0.8
    min_mutation_score: float = 0.6
    protected_paths: list[str] = field(default_factory=list)
    forbid_new_doc_files: bool = True
    doc_file_patterns: list[str] = field(default_factory=lambda: ["*.md", "*.rst", "*.adoc", "docs/**"])
    test_file_patterns: list[str] = field(default_factory=lambda: ["test_*", "*_test.*", "*.test.*", "*.spec.*", "tests/**", "test/**"])


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
    harnesses: dict[str, HarnessConfig] = field(default_factory=dict)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    gates: GatesConfig = field(default_factory=GatesConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
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
    gates: dict[str, object] = field(default_factory=dict)
    skills: dict[str, object] = field(default_factory=dict)
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


@dataclass(frozen=True)
class RepoCommands:
    """Each entry is one argv list. Language-agnostic: the repo says how it is
    linted, tested, and verified; the factory only runs the commands."""

    setup: list[list[str]] = field(default_factory=list)
    lint: list[list[str]] = field(default_factory=list)
    test: list[list[str]] = field(default_factory=list)
    verify: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class MutationRules:
    """command produces report; format stryker (mutation-testing-report-schema) or
    score ({"score": 0.0-1.0}). Empty command means mutation is not measured."""

    command: list[str] = field(default_factory=list)
    report: str = ""
    format: Literal["stryker", "score"] = "stryker"


@dataclass(frozen=True)
class RepoRules:
    """Per-repo layer, read from `.factory/rules.toml` in the repo. It may add
    rules and deny patterns, never relax global ones."""

    standards: list[str] = field(default_factory=list)
    commands: RepoCommands = field(default_factory=RepoCommands)
    evidence: list[str] = field(default_factory=list)
    extra_deny_patterns: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    command_timeout_seconds: int = 900
    coverage_report: str = ""
    mutation: MutationRules = field(default_factory=MutationRules)
