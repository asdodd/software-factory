"""Cross-field validation. Runs without connecting to anything."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from factory.config.registry import PluginError, resolve_class
from factory.config.schema import AgentConfig, CommenterRule, FactoryConfig, FilterConfig
from factory.features.definitions import FEATURES_BY_NAME
from factory.features.resolver import FeatureStates

if TYPE_CHECKING:
    from factory.config.loader import Diagnostic, LoadedConfig

MODEL_STAGES = {
    "triage": ("triage",),
    "spec": ("spec",),
    "implement": ("implement",),
    "review": ("review",),
    "intake": ("intake.email",),
    "grading": ("feedback.grading",),
    "respond": ("mr.respond", "mr.auto_fix"),
    "improve": ("feedback.self_improvement",),
}

ALWAYS_PORTS = ("ticket_system", "git_host", "sandbox", "access_guard", "accounting", "state_store", "clock", "id_source",
                "notifier")
FEATURE_PORTS = {"mailbox": ("intake.email",), "monitor": ("intake.monitoring", "monitoring")}
TOOL_LOOP_HARNESSES = frozenset({"harness.builtin"})


def effective_rule(config: FactoryConfig, channel: str) -> CommenterRule:
    collab = config.collaboration
    override = collab.jira_allowed_commenters if channel == "jira" else collab.mr_allowed_commenters
    return override if override is not None else collab.allowed_commenters


def rule_is_empty(rule: CommenterRule) -> bool:
    return not (rule.users or rule.groups or rule.project_roles or rule.include_reporter or rule.include_assignee)


def _disjoint(a: Iterable[str], b: Iterable[str]) -> bool:
    a, b = set(a), set(b)
    return bool(a) and bool(b) and not (a & b)


def filters_may_overlap(a: FilterConfig, b: FilterConfig) -> bool:
    """False only when no ticket can match both filters."""
    for dim in ("projects", "issue_types", "priorities", "statuses"):
        if _disjoint(getattr(a, dim), getattr(b, dim)):
            return False
    for x, y in ((a, b), (b, a)):
        if set(x.labels_all) & set(y.labels_none):
            return False
        if x.labels_any and set(x.labels_any) <= set(y.labels_none):
            return False
    for name in set(a.custom_fields) & set(b.custom_fields):
        if _disjoint(a.custom_fields[name], b.custom_fields[name]):
            return False
    return True


def overlapping_agents(agents: dict[str, AgentConfig]) -> list[tuple[str, str]]:
    names = sorted(n for n, a in agents.items() if a.enabled)
    return [
        (x, y)
        for i, x in enumerate(names)
        for y in names[i + 1:]
        if filters_may_overlap(agents[x].filter, agents[y].filter)
    ]


def required_ports(features: FeatureStates) -> list[str]:
    ports = list(ALWAYS_PORTS)
    for port, needs in FEATURE_PORTS.items():
        if any(features.on(f) for f in needs):
            ports.append(port)
    return ports


def active_stages(features: FeatureStates) -> list[str]:
    return [stage for stage, needs in MODEL_STAGES.items() if any(features.on(f) for f in needs)]


def _check_one(label: str, cfg: FactoryConfig, features: FeatureStates, out: list) -> None:
    from factory.config.loader import Diagnostic

    prefix = f"{label}: " if label else ""
    for problem in features.problems():
        out.append(Diagnostic("error", prefix + problem))
    for name in cfg.features:
        feature = FEATURES_BY_NAME.get(name)
        if feature is None or not feature.config_path.startswith("features."):
            out.append(Diagnostic("error", f"{prefix}features.{name}: unknown feature"))
    for port in required_ports(features):
        selection = getattr(cfg.plugins, port)
        if not selection.adapter:
            out.append(Diagnostic("error", f"{prefix}plugins.{port}.adapter is required"))
    for stage in active_stages(features):
        sm = getattr(cfg.models.stages, stage)
        if not sm.provider:
            out.append(Diagnostic("error", f"{prefix}models.stages.{stage}.provider is required"))
        elif sm.provider not in cfg.models.providers:
            out.append(Diagnostic("error", f"{prefix}models.stages.{stage}.provider: unknown provider {sm.provider!r}"))
        if not sm.model:
            out.append(Diagnostic("error", f"{prefix}models.stages.{stage}.model is required"))
        if stage == "implement" and not sm.harness:
            out.append(Diagnostic("error", f"{prefix}models.stages.implement.harness is required"))
        if sm.harness and sm.harness not in cfg.harnesses:
            out.append(Diagnostic("error", f"{prefix}models.stages.{stage}.harness: unknown harness {sm.harness!r}"))
        elif sm.harness:
            _check_harness(prefix, sm.harness, cfg, out)
    stages = cfg.models.stages
    if features.on("review") and (stages.review.provider, stages.review.model) == (
            stages.implement.provider, stages.implement.model):
        out.append(Diagnostic("warning", f"{prefix}review uses the same provider and model as implement; "
                                         "a different model avoids correlated blind spots"))
    if (features.on("jira_comments.read_replies") or features.on("mr.read_comments")) and not cfg.collaboration.factory_identity:
        out.append(Diagnostic("error", f"{prefix}collaboration.factory_identity is required to ignore the factory's own comments"))
    for channel, needs in (("jira", "jira_comments.read_replies"), ("mr", "mr.read_comments")):
        if features.on(needs) and rule_is_empty(effective_rule(cfg, channel)):
            out.append(Diagnostic(
                "warning", f"{prefix}{needs} is on but the allowed-commenter rule is empty: all replies will be ignored"
            ))


def _check_harness(prefix: str, name: str, cfg: FactoryConfig, out: list) -> None:
    from factory.config.loader import Diagnostic

    h = cfg.harnesses[name]
    try:
        cls = resolve_class(h.adapter, cfg.plugins.aliases)
    except PluginError as exc:
        out.append(Diagnostic("error", f"{prefix}harnesses.{name}.adapter: {exc}"))
        return
    validate = getattr(cls, "validate_settings", None)
    for problem in validate(h.settings) if validate else ():
        out.append(Diagnostic("error", f"{prefix}harnesses.{name}.{problem}"))
    dialect = getattr(cls, "required_dialect", None)
    needed = dialect(h.settings) if dialect else None
    if needed and needed not in cfg.gateway.dialects:
        out.append(Diagnostic("error", f"{prefix}harness {name!r} needs gateway dialect {needed}; "
                                       "add it to gateway.dialects"))
    if cfg.access_policy.mode == "deny_read" and h.adapter not in TOOL_LOOP_HARNESSES and not _sandbox_hides_paths(cfg):
        out.append(Diagnostic(
            "error",
            f"{prefix}access_policy.mode = deny_read cannot be enforced for harness {name!r}; "
            "use mode = strip or a sandbox that hides paths",
        ))


def _sandbox_hides_paths(cfg: FactoryConfig) -> bool:
    try:
        cls = resolve_class(cfg.plugins.sandbox.adapter, cfg.plugins.aliases)
    except PluginError:
        return False
    return bool(getattr(cls, "HIDES_PATHS", False))


OPTIONAL_EMPTY = frozenset({"ca_bundle_path", "session_token", "prefix", "kms_key_id", "region", "default_project",
                            "default_issue_type", "spec_approved", "stalled", "template_path"})


def _empty_values(data: object, path: str = "") -> list[str]:
    found = []
    if isinstance(data, dict):
        for key, value in data.items():
            here = f"{path}.{key}" if path else str(key)
            if value == "" and key not in OPTIONAL_EMPTY:
                found.append(here)
            else:
                found.extend(_empty_values(value, here))
    return found


def check(loaded: LoadedConfig) -> list[Diagnostic]:
    from factory.config.loader import Diagnostic

    cfg = loaded.config
    out: list[Diagnostic] = []
    if loaded.profile != "sim":
        needed = set(required_ports(loaded.features))
        for name in loaded.enabled_agents():
            needed |= set(required_ports(loaded.agent_features[name]))
        for layer in loaded.layers:
            if layer.source.startswith("file:profile:"):
                for path in _empty_values(layer.data):
                    parts = path.split(".")
                    if parts[0] == "plugins" and parts[1] not in needed:
                        continue
                    out.append(Diagnostic("error", f"{path} is empty in profile {loaded.profile}: fill it in to go live"))
    _check_one("", cfg, loaded.features, out)
    for name, agent in loaded.agents.items():
        if not agent.enabled:
            continue
        _check_one(f"agent {name}", loaded.agent_configs[name], loaded.agent_features[name], out)
        if not agent.service_account:
            out.append(Diagnostic("error", f"agent {name}: service_account is required"))
        if bool(agent.filter.native_query) != bool(agent.filter.native_adapter):
            out.append(Diagnostic("error", f"agent {name}: filter.native_query and filter.native_adapter go together"))
        if not loaded.agent_configs[name].labels.ready:
            out.append(Diagnostic("error", f"agent {name}: labels.ready is required"))

    ts = cfg.trace_store
    if loaded.features.on("feedback.trace_store") and ts.backend == "s3":
        if not ts.s3.endpoint_url:
            out.append(Diagnostic("error", "trace_store.s3.endpoint_url is required for the s3 backend (no implicit endpoint)"))
        if not ts.s3.bucket:
            out.append(Diagnostic("error", "trace_store.s3.bucket is required for the s3 backend"))
        if ts.require_encryption and not ts.s3.encryption.mode:
            out.append(Diagnostic("error", "trace_store.s3.encryption.mode is required when require_encryption is true"))
        if ts.s3.encryption.mode == "sse-kms" and not ts.s3.encryption.kms_key_id:
            out.append(Diagnostic("error", "trace_store.s3.encryption.kms_key_id is required for sse-kms"))

    if loaded.features.on("feedback.self_improvement") and not cfg.feedback.self_improvement.repo:
        out.append(Diagnostic("error", "feedback.self_improvement.repo is required when self-improvement is on"))
    if not cfg.access_policy.enabled:
        out.append(Diagnostic("alert", "access_policy.enabled is false: the no-proprietary-files base skill is NOT enforced"))
    if cfg.intake.email.allow_updates:
        out.append(Diagnostic("warning", "intake.email.allow_updates is true: email replies can update tickets"))

    routing = cfg.intake.routing
    if loaded.features.on("intake.routing"):
        for i, rule in enumerate(routing.rules):
            if rule.agent not in loaded.agents:
                out.append(Diagnostic("error", f"intake.routing.rules[{i}].agent: unknown agent {rule.agent!r}"))
    for x, y in overlapping_agents(loaded.agents):
        out.append(Diagnostic("warning", f"agents {x} and {y} have filters that may overlap; leases prevent double claims"))
    return out
