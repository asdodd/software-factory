"""Single source of truth for feature toggles.

Each feature is a boolean at `config_path`. `requires` lists features that must
also be on; `requires_output_mode` constrains the non-boolean output.mode.
The access policy is deliberately absent: it is not a normal toggle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    name: str
    default: bool
    description: str
    config_path: str
    requires: tuple[str, ...] = ()
    requires_output_mode: str | None = None
    per_agent: bool = True


def _f(name: str, default: bool, description: str, *, path: str | None = None,
       requires: tuple[str, ...] = (), mode: str | None = None, per_agent: bool = True) -> Feature:
    return Feature(name, default, description, path or f"features.{name}", requires, mode, per_agent)


FEATURES: tuple[Feature, ...] = (
    _f("intake.jira", True, "Accept tickets created or labeled in the ticket system.", per_agent=False),
    _f("intake.email", False, "Draft tickets from the bot mailbox.", per_agent=False),
    _f("intake.email.auto_label", False, "Apply the ready label for allowlisted senders; off means draft-only.",
       path="intake.email.auto_label", requires=("intake.email",), per_agent=False),
    _f("intake.email.allow_updates", False, "Let email replies update existing tickets or agent context.",
       path="intake.email.allow_updates", requires=("intake.email",), per_agent=False),
    _f("intake.gitlab_comments", False, "Create follow-up tickets from a keyword in MR or issue comments.", per_agent=False),
    _f("intake.monitoring", False, "Accept monitoring findings as intake events.", per_agent=False),
    _f("intake.routing", False, "Route new tickets to an agent by applying its ready label.",
       path="intake.routing.enabled", per_agent=False),
    _f("readiness_gate", True, "Check tickets against the shared readiness checklist."),
    _f("triage", True, "Model classifies tickets as easy or hard."),
    _f("spec", True, "Hard tickets get a product and tech spec, approved by a human.", requires=("triage",)),
    _f("implement", True, "Worker implements the ticket in a sandbox."),
    _f("review", True, "Separate model or context reviews the implementation.", requires=("implement",)),
    _f("verify", True, "Produce verification evidence from the real code path.", requires=("implement",)),
    _f("monitoring", False, "Run regression and production monitors.", requires=("intake.monitoring",), per_agent=False),
    _f("feedback.metrics", True, "Emit pipeline metrics."),
    _f("feedback.trace_store", True, "Persist traces, plans, and outcomes."),
    _f("feedback.grading", False, "Grade traces with a cheaper model.", requires=("feedback.trace_store",), per_agent=False),
    _f("feedback.self_improvement", False, "Propose factory changes as MRs from grades and traces.",
       requires=("feedback.grading",), per_agent=False),
    _f("jira_comments.post", True, "Comment on the ticket at milestones.", path="jira_comments.post"),
    _f("jira_comments.read_replies", True, "Poll human replies on the ticket and act on them.",
       path="jira_comments.read_replies", requires=("jira_comments.post",)),
    _f("mr.read_comments", True, "Poll the agent's own MRs for reviewer comments.",
       path="mr.read_comments", mode="merge_request"),
    _f("mr.respond", True, "Reply in MR threads.", path="mr.respond",
       requires=("mr.read_comments",), mode="merge_request"),
    _f("mr.auto_fix", True, "Push fix commits to the agent's own MR branch for change requests.",
       path="mr.auto_fix", requires=("mr.read_comments",), mode="merge_request"),
    _f("git.submodules", True, "Initialize and verify submodules on clone and pull.", path="git.submodules.enabled"),
    _f("dry_run", False, "Log intended external writes instead of performing them."),
)

FEATURES_BY_NAME: dict[str, Feature] = {f.name: f for f in FEATURES}
