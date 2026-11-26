"""Renders the instructions file every harness receives. Base skills and the
global process rules are always included and cannot be removed."""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from pathlib import Path

from factory.config.schema import FactoryConfig, RepoRules
from factory.core.readiness import text_field
from factory.ports.types import Ticket

SKILLS = Path(__file__).resolve().parents[1] / "skills"


@cache
def base_skills() -> str:
    return "\n\n".join(p.read_text().strip() for p in sorted((SKILLS / "base").glob("*.md")))


def render(ticket: Ticket, cfg: FactoryConfig, rules: RepoRules, standards: Sequence[tuple[str, str]],
           spec: str = "", feedback: Sequence[str] = (), withheld_attachments: int = 0,
           attachments_dir: str = "", answers: Sequence[str] = (), skills_text: str = "") -> str:
    parts = [
        "# Factory task",
        base_skills(),
        "## Limits",
        f"- At most {cfg.limits.max_files_changed} files changed and {cfg.limits.max_diff_lines} diff lines.",
        "- The change is checked by mechanical gates: lint, tests, protected paths, access policy, and size.",
        f"## Ticket {ticket.key}: {ticket.summary}",
        ticket.description.strip() or "(no description)",
        "### Acceptance criteria",
        text_field(ticket, "acceptance_criteria") or "(none stated)",
    ]
    verify = text_field(ticket, "verify_steps")
    if verify:
        parts += ["### How to verify", verify]
    if spec:
        parts += ["## Approved spec", spec]
    if answers:
        parts += ["## Answers from the ticket", *answers]
    commands = rules.commands
    listed = [("lint", commands.lint), ("test", commands.test), ("verify", commands.verify)]
    lines = [f"- {name}: {' '.join(cmd)}" for name, cmds in listed for cmd in cmds]
    if lines:
        parts += ["## Repo commands", *lines]
    for name, text in standards:
        parts += [f"## Repo standards: {name}", text.strip()]
    if attachments_dir:
        parts += ["## Attachments", f"Readable attachments are in {attachments_dir}."]
    if withheld_attachments:
        parts.append(f"{withheld_attachments} attachment(s) were withheld by the access policy.")
    if skills_text:
        parts.append(skills_text)
    if feedback:
        parts += ["## Previous attempt failed", *(f"- {f}" for f in feedback)]
    parts += ["## When finished",
              "Report status done, needs_info (with numbered questions), blocked, or failed, with a short summary."]
    return "\n\n".join(parts) + "\n"
