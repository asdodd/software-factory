"""Scheduled self-improvement: read metrics, calibrated grades, and the worst
runs, then propose changes to the factory repo as a merge request. It never
applies anything itself, and only files under feedback.self_improvement
.allowed_paths may change (skills and agent definitions by default; never the
global config or the access policy). A retro section suggests new automated
checks and coding-standard updates for humans to consider."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from factory.core import metrics_report, workspace
from factory.core.budget import BudgetedModels
from factory.core.container import Container
from factory.core.gates import glob_match
from factory.core.grading import Grader
from factory.core.writes import Writes
from factory.ports.types import (
    CallContext,
    MergeRequestDraft,
    Message,
    RepoRef,
    Role,
    TraceQuery,
    WorkspaceSpec,
)
from factory.vcs.git import Git, GitOptions

log = logging.getLogger("factory.improve")
AGENT = "self-improvement"
PROMPT = (
    "You improve a shared software factory. From the metrics, grades, and run notes, propose small, specific "
    "changes to the files shown (skills and agent definitions). Reply with JSON "
    '{"summary": "...", "rationale": "...", "edits": [{"path": "...", "content": "<full new file content>"}], '
    '"retro": {"suggested_checks": ["..."], "standards_updates": ["..."]}}. '
    "Only edit the files you were shown. Never weaken safety rules or the access policy."
)


@dataclass(frozen=True)
class Proposal:
    opened: bool
    reason: str
    mr_url: str = ""
    files: tuple[str, ...] = ()


class SelfImprovement:
    def __init__(self, container: Container) -> None:
        self.c = container
        self.cfg = container.loaded.config
        self.si = self.cfg.feedback.self_improvement
        self.clock = container.port("clock")
        self.git = container.port("git_host")
        self.sandbox = container.port("sandbox")
        self.guard = container.port("access_guard").derive(self.cfg.access_policy.deny_patterns)
        self.writes = Writes(container.port("ticket_system"), self.git, container.port("state_store"), self.clock,
                             container.redactor, self.cfg, AGENT, container.loaded.features.on("dry_run"))

    def run(self) -> Proposal:
        grader = Grader(self.c)
        calibration = grader.calibration()
        if calibration is None or not calibration.trusted:
            return Proposal(False, "the grader is not calibrated against human judgments; run `factory grade --calibrate`")
        now = self.clock.now()
        since = now - timedelta(days=self.si.lookback_days)
        store = self.c.trace_store()
        report = metrics_report.compute(store, self.c.port("state_store"), since, now)
        grades = []
        for meta in store.list(TraceQuery(since=since, until=now)):
            for o in store.get(meta.run_id)[2]:
                if o.kind == "grade" and o.payload.get("calibrated"):
                    grades.append({"run": meta.run_id, "agent": meta.agent, **o.payload})
        if not grades:
            return Proposal(False, "no calibrated grades in the lookback window")
        repo = RepoRef("", self.si.repo, self.si.base_branch)
        ws = self.sandbox.prepare(WorkspaceSpec(f"improve-{now:%Y%m%d%H%M%S}", AGENT, "factory"))
        try:
            clone = ws.root / "clone"
            self.git.clone(repo, clone, self.si.base_branch)
            self.guard.scan_workspace(clone)
            files = self._allowed_files(clone)
            proposal = self._ask(report, grades, files)
            return self._open(repo, clone, proposal, report, grades)
        finally:
            self.sandbox.teardown(ws)

    def _allowed_files(self, clone: Path) -> dict[str, str]:
        out = {}
        for p in sorted(clone.rglob("*")):
            rel = str(p.relative_to(clone))
            if p.is_file() and ".git" not in p.parts and glob_match(rel, self.si.allowed_paths) and \
                    self.guard.check_path(clone, rel).allowed:
                out[rel] = p.read_text(errors="replace")[:20000]
        return out

    def _ask(self, report: dict, grades: list[dict], files: dict[str, str]) -> dict:
        worst = sorted(grades, key=lambda g: (-g["wasted_tool_calls"], -g["verbosity"]))[:10]
        shown = "\n\n".join(f"--- {path}\n{text}" for path, text in files.items())
        user = (f"Metrics:\n{json.dumps(report, sort_keys=True)}\n\nWorst graded runs:\n"
                f"{json.dumps(worst, sort_keys=True)}\n\nFiles you may edit:\n{shown}")
        stage = self.cfg.models.stages.improve
        models = BudgetedModels(self.c.provider, self.c.port("accounting"), self.clock,
                                self.cfg.limits.max_tokens_per_ticket)
        raw = models.complete(stage.provider, stage.model, (Message(Role.SYSTEM, PROMPT), Message(Role.USER, user)),
                              CallContext(AGENT, f"improve:{self.clock.now():%Y%m%d}", "improve", "factory",
                                          "factory")).message.content
        try:
            doc = json.loads(raw)
            return doc if isinstance(doc, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _open(self, repo: RepoRef, clone: Path, proposal: dict, report: dict, grades: list[dict]) -> Proposal:
        edits, dropped = [], []
        for e in proposal.get("edits") or []:
            path = str(e.get("path", "")) if isinstance(e, dict) else ""
            if (not path or not glob_match(path, self.si.allowed_paths) or ".." in Path(path).parts
                    or not self.guard.check_diff_paths([path]).allowed
                    or self.guard.content_matches(str(e.get("content", "")).encode())):
                dropped.append(path or "(unnamed)")
                continue
            edits.append((path, str(e.get("content", ""))))
        edits = edits[: self.si.max_files]
        retro = proposal.get("retro") or {}
        if not edits and not (retro.get("suggested_checks") or retro.get("standards_updates")):
            return Proposal(False, "the model proposed nothing usable" + (f" (dropped: {dropped})" if dropped else ""))
        branch = self.cfg.output.branch_pattern.format(agent=AGENT, ticket_key=f"{self.clock.now():%Y%m%d}")
        git = Git(GitOptions(clone_depth=0, submodules_enabled=False, author_name=AGENT,
                             author_email=f"{AGENT}@factory.invalid", allow_protocols="file"))
        git.run(["checkout", "-q", "-B", branch], cwd=clone)
        for path, content in edits:
            (clone / path).parent.mkdir(parents=True, exist_ok=True)
            (clone / path).write_text(content)
        if edits:
            workspace.stage_all(git, clone, [p for p, _ in edits])
            if not git.out(["diff", "--cached", "--name-only"], cwd=clone):
                edits = []
        if edits:
            git.run(["commit", "-q", "-m", f"Self-improvement proposal\n\n{proposal.get('summary', '')}"], cwd=clone)
        else:
            git.run(["commit", "-q", "--allow-empty", "-m", "Self-improvement retro (no file changes)"], cwd=clone)
        self.writes.push_branch(clone, branch)
        description = self._description(proposal, report, grades, [p for p, _ in edits], dropped)
        mr = self.writes.open_merge_request(MergeRequestDraft(
            repo, branch, self.si.base_branch, f"Self-improvement proposal {self.clock.now():%Y-%m-%d}", description,
            f"improve:{self.clock.now():%Y%m%d}"))
        return Proposal(True, "opened", mr.url, tuple(p for p, _ in edits))

    def _description(self, proposal: dict, report: dict, grades: list[dict], files: list[str],
                     dropped: list[str]) -> str:
        retro = proposal.get("retro") or {}
        checks = "\n".join(f"- {c}" for c in retro.get("suggested_checks") or []) or "- (none)"
        standards = "\n".join(f"- {c}" for c in retro.get("standards_updates") or []) or "- (none)"
        changed = "\n".join(f"- {f}" for f in files) or "- (no file changes; retro only)"
        return "\n\n".join([
            f"## What changed\n{proposal.get('summary', '')}\n\n{changed}"
            + (f"\n\nDropped edits outside the allowed paths: {', '.join(dropped)}" if dropped else ""),
            f"## Why\n{proposal.get('rationale', '')}\n\nBased on {len(grades)} calibrated grade(s) and metrics:\n"
            f"```json\n{json.dumps(report, indent=2, sort_keys=True)}\n```",
            "## Blast radius\nFactory skills and agent definitions only. Nothing is applied until a human merges.",
            "## How verified\nThe factory repo's CI runs on this MR. The proposal was generated, not tested.",
            "## Architecture diff\nNo structural change.",
            f"## Retro\n### Suggested automated checks\n{checks}\n\n### Coding-standard updates\n{standards}",
        ])
