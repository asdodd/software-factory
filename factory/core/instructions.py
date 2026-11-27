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


def _commands(rules: RepoRules) -> list[str]:
    commands = rules.commands
    listed = [("lint", commands.lint), ("test", commands.test), ("verify", commands.verify)]
    return [f"- {name}: {' '.join(cmd)}" for name, cmds in listed for cmd in cmds]


def render(ticket: Ticket, cfg: FactoryConfig, rules: RepoRules, standards: Sequence[str],
           spec: str = "", feedback: Sequence[str] = (), withheld_attachments: int = 0,
           attachments_dir: str = "", answers: Sequence[str] = (),
           repos: Sequence[tuple[str, str, RepoRules]] = ()) -> str:
    """standards: paths of standards files in the working directory (the agent
    reads them itself). repos: (path, directory, rules) when the ticket spans
    several repos; empty for one repo, whose copy is the working directory."""
    parts = [
        "# Factory task",
        base_skills(),
        "## Limits",
        f"- At most {cfg.limits.max_files_changed} files changed and {cfg.limits.max_diff_lines} diff lines.",
        "- The change is checked by mechanical gates: lint, tests, protected paths, and size.",
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
    if repos:
        parts += ["## Repos",
                  "This ticket spans several repos. Each is a directory in your working directory; "
                  "change only the ones the ticket needs. Each repo's commands run inside its own directory.",
                  *(f"- {path}: ./{directory}/" for path, directory, _ in repos)]
        for path, directory, repo_rules in repos:
            lines = _commands(repo_rules)
            if lines:
                parts += [f"## Repo commands: {path} (run in ./{directory}/)", *lines]
    else:
        lines = _commands(rules)
        if lines:
            parts += ["## Repo commands", *lines]
    if not any(_commands(r) for r in ([rr for _, _, rr in repos] if repos else [rules])):
        parts += ["## Repo commands",
                  "None are configured in the factory. Find and run the repo's own tooling, as the base skill says."]
    if standards:
        parts += ["## Repo standards", "Read and follow these files:", *(f"- {s}" for s in standards)]
    if attachments_dir:
        parts += ["## Attachments", f"Readable attachments are in {attachments_dir}."]
    if withheld_attachments:
        parts.append(f"{withheld_attachments} attachment(s) were withheld.")
    if feedback:
        parts += ["## Previous attempt failed", *(f"- {f}" for f in feedback)]
    parts += ["## When finished",
              "Report status done, needs_info (with numbered questions), blocked, or failed, with a short summary."]
    return "\n\n".join(parts) + "\n"
