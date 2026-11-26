"""The single shared readiness checklist (factory/skills/readiness.toml),
used by intake and re-checked by workers. Deterministic: no model."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from factory.config.schema import LimitsConfig, RepoRules
from factory.ports.types import Ticket

CHECKLIST = Path(__file__).resolve().parents[1] / "skills" / "readiness.toml"
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)]|\[[ xX]?\])\s+\S")


@dataclass(frozen=True)
class Readiness:
    ready: bool
    failures: tuple[str, ...]


@cache
def checklist() -> tuple[tuple[str, str, str], ...]:
    with CHECKLIST.open("rb") as fh:
        return tuple((c["id"], c["rule"], c["message"]) for c in tomllib.load(fh)["checks"])


def text_field(ticket: Ticket, name: str) -> str:
    value = ticket.fields.get(name)
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {v}" for v in value)
    return str(value or "").strip()


def acceptance_items(ticket: Ticket) -> list[str]:
    text = text_field(ticket, "acceptance_criteria")
    items = [line for line in text.splitlines() if _ITEM.match(line)]
    return items or ([text] if text else [])


def evaluate(ticket: Ticket, limits: LimitsConfig, repo_scope: list[str] | None = None,
             rules: RepoRules | None = None) -> Readiness:
    """rules=None means the repo has not been read yet; a verify check that
    could be satisfied by the repo rules then passes provisionally."""
    failures = []
    for _id, rule, message in checklist():
        if rule == "repo_identified":
            repo = text_field(ticket, "repo")
            if not repo or (repo_scope is not None and repo not in repo_scope):
                failures.append(message if not repo else f"The repo {repo} is not in this agent's scope.")
        elif rule == "acceptance_criteria_stated":
            if not acceptance_items(ticket):
                failures.append(message)
        elif rule == "verification_known":
            if not text_field(ticket, "verify_steps") and rules is not None and not (
                rules.commands.verify or rules.commands.test
            ):
                failures.append(message)
        elif rule == "scope_bounded":
            size = len(ticket.summary) + len(ticket.description) + len(text_field(ticket, "acceptance_criteria"))
            if size > limits.max_ticket_chars or len(acceptance_items(ticket)) > limits.max_acceptance_criteria:
                failures.append(message)
        else:
            raise ValueError(f"unknown readiness rule {rule!r}")
    return Readiness(not failures, tuple(failures))
