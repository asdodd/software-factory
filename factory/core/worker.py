"""One agent's pipeline: claim, readiness, triage, spec, clone, isolate,
implement (any harness), gates, verify, review, publish, and trace.

Every external write goes through Writes; every model call goes through
BudgetedModels; every file the harness sees has passed the access policy."""

from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from factory.config.schema import RepoRules
from factory.config.validate import ConfigError, build
from factory.core import gates, instructions, readiness, workspace
from factory.core.budget import BudgetedModels
from factory.core.claim import Claimer
from factory.core.container import Container
from factory.core.gateway import Gateway
from factory.core.recorder import Recorder
from factory.core.writes import Note, Writes
from factory.ports.errors import BudgetExceeded, NotFound, SubmoduleFailure
from factory.ports.types import (
    Artifact,
    CallContext,
    Command,
    CommandResult,
    HarnessStatus,
    HarnessTask,
    LogicalStatus,
    MergeRequestDraft,
    Message,
    ModelEndpoint,
    Notification,
    NotificationKind,
    Outcome,
    RepoRef,
    Role,
    Ticket,
    Workspace,
    WorkspaceSpec,
)
from factory.vcs.git import Git, GitOptions

log = logging.getLogger("factory.worker")
PATH_TOKEN = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}(?![\w/-])|(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+")


@dataclass
class RunReport:
    ticket_key: str
    outcome: Outcome
    detail: str = ""
    run_id: str = ""
    mr_url: str = ""
    branch: str = ""


@dataclass
class SandboxRuntime:
    sandbox: object
    workspace: Workspace
    subdir: str
    guard: object
    max_denials: int

    def run(self, command: Command) -> CommandResult:
        cwd = self.subdir if command.cwd in ("", ".") else f"{self.subdir}/{command.cwd}"
        return self.sandbox.run(self.workspace, Command(command.argv, cwd, command.env, command.timeout_seconds,
                                                         command.stdin_path))


@dataclass
class Attempt:
    feedback: list[str] = field(default_factory=list)


class Stop(Exception):
    def __init__(self, outcome: Outcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


class Worker:
    def __init__(self, container: Container, agent_name: str, worker_id: str | None = None) -> None:
        loaded = container.loaded
        self.c = container
        self.name = agent_name
        self.agent = loaded.agents[agent_name]
        self.cfg = loaded.agent_configs[agent_name]
        self.features = loaded.agent_features[agent_name]
        self.config_hash = loaded.config_hash
        self.tickets = container.port("ticket_system")
        self.git = container.port("git_host")
        self.sandbox = container.port("sandbox")
        self.state = container.port("state_store")
        self.clock = container.port("clock")
        self.ids = container.port("id_source")
        self.accounting = container.port("accounting")
        self.base_guard = container.port("access_guard")
        self.metrics = container.metrics
        self.worker_id = worker_id or f"{agent_name}:{self.ids.new_id('worker')}"
        self.dry_run = self.features.on("dry_run")
        self.writes = Writes(self.tickets, self.git, self.state, self.clock, container.redactor, self.cfg,
                             agent_name, self.dry_run)
        self.claimer = Claimer(self.tickets, self.writes, self.clock, self.agent, self.cfg, self.worker_id,
                               self.metrics, self.dry_run)
        self.tickets.validate_filter(self.claimer.filter)

    # ---- loop ------------------------------------------------------------
    def run_once(self) -> list[RunReport]:
        reports = []
        for ticket, mode in self.claimer.candidates():
            if len(reports) >= self.agent.claim.max_in_flight:
                break
            claimed = self.claimer.claim(ticket.key, mode)
            if claimed is None:
                reports.append(RunReport(ticket.key, Outcome.CLAIM_LOST, "not claimed"))
                continue
            reports.append(self.process(claimed, mode))
        return [r for r in reports if r.outcome is not Outcome.CLAIM_LOST] + [
            r for r in reports if r.outcome is Outcome.CLAIM_LOST]

    def process(self, ticket: Ticket, mode: str) -> RunReport:
        run_id = self.ids.new_id("run")
        recorder = Recorder(self.c.trace_writer(self.features.on("feedback.trace_store")), self.c.redactor, self.clock,
                            self.metrics, self.cfg.trace_store.capture_level, run_id=run_id, agent=self.name,
                            ticket_key=ticket.key, config_hash=self.config_hash)
        recorder.repo = readiness.text_field(ticket, "repo")
        models = BudgetedModels(self.c.provider, self.accounting, self.clock, self.cfg.limits.max_tokens_per_ticket,
                                recorder)
        run = _Run(self, ticket, mode, run_id, recorder, models)
        try:
            report = run.execute()
        except Stop as stop:
            report = RunReport(ticket.key, stop.outcome, stop.detail, run_id)
        except BudgetExceeded as exc:
            report = run.fail(f"Spend cap reached: {exc}")
        except Exception as exc:  # noqa: BLE001 - any failure ends in a failed ticket, never a crash loop
            log.exception("run %s failed", run_id)
            report = run.fail(f"Internal error ({type(exc).__name__}). See the trace for run {run_id}.")
        finally:
            run.cleanup()
        recorder.event("outcome", {"outcome": report.outcome.value, "detail": report.detail}, always=True)
        recorder.finish(report.outcome)
        self.metrics.incr("runs", outcome=report.outcome.value, agent=self.name)
        return report


class _Run:
    def __init__(self, worker: Worker, ticket: Ticket, mode: str, run_id: str, recorder: Recorder,
                 models: BudgetedModels) -> None:
        self.w = worker
        self.cfg = worker.cfg
        self.ticket = ticket
        self.key = ticket.key
        self.mode = mode
        self.run_id = run_id
        self.rec = recorder
        self.models = models
        self.guard = worker.base_guard.derive(self.cfg.access_policy.deny_patterns)
        self.ws: Workspace | None = None
        self.checkout = None
        self.rules = RepoRules()
        self.reporter = ticket.reporter.id if ticket.reporter else ""
        self.team = str(ticket.fields.get("sponsor_team") or worker.agent.team)

    # ---- helpers -----------------------------------------------------------
    def say(self, milestone: str, note: Note) -> None:
        self.w.writes.comment(self.key, milestone, note, f"{self.run_id}:{milestone}")

    def context(self, stage: str) -> CallContext:
        return CallContext(self.w.name, self.key, stage, self.reporter or self.w.agent.service_account, self.team)

    def ask_model(self, stage: str, system: str, user: str) -> str:
        sm = getattr(self.cfg.models.stages, stage)
        resp = self.models.complete(sm.provider, sm.model,
                                    (Message(Role.SYSTEM, system), Message(Role.USER, user)), self.context(stage))
        return resp.message.content

    def release(self) -> None:
        self.w.claimer.release(self.key)

    def labels(self, add=(), remove=()) -> None:
        self.w.writes.set_labels(self.key, add, remove)

    def fail(self, summary: str) -> RunReport:
        labels = self.cfg.labels
        self.w.writes.transition(self.key, LogicalStatus.FAILED)
        self.labels(add=[labels.failed], remove=[labels.ready])
        self.release()
        self.say("failed", Note("Failed", "failed", done=summary,
                                next="A human should review the ticket and the trace before retrying."))
        return RunReport(self.key, Outcome.FAILED, summary, self.run_id)

    def blocked(self, summary: str, paths: list[str] | None = None) -> Stop:
        self.w.writes.transition(self.key, LogicalStatus.BLOCKED)
        self.release()
        detail = summary + ("\nPaths: " + ", ".join(sorted(paths)) if paths else "")
        self.say("blocked", Note("Blocked", "blocked", done=detail, next="Needs a human decision."))
        return Stop(Outcome.BLOCKED, detail)

    def needs_info(self, questions: tuple[str, ...], why: str) -> Stop:
        jc = self.cfg.jira_comments
        if jc.needs_info_action == "status":
            self.w.writes.transition(self.key, LogicalStatus.NEEDS_INFO)
        self.labels(add=[self.cfg.labels.needs_info])
        if jc.lease_while_paused == "release":
            self.release()
        self.w.state.put("paused", self.key, json.dumps({"agent": self.w.name, "run_id": self.run_id,
                                                         "questions": list(questions),
                                                         "since": self.w.clock.now().isoformat()}))
        self.say("needs_info", Note("Needs more information", "paused", done=why,
                                    next="Answer the questions in a comment on this ticket.",
                                    questions=questions or ("Please clarify the ticket.",)))
        return Stop(Outcome.NEEDS_INFO, "; ".join(questions))

    def cleanup(self) -> None:
        if self.ws is not None:
            self.w.sandbox.teardown(self.ws)

    # ---- stages ------------------------------------------------------------
    def execute(self) -> RunReport:
        features = self.w.features
        title = "Reclaimed after lease expiry" if self.mode == "reclaim" else "Claimed"
        self.say("reclaimed" if self.mode == "reclaim" else "claimed",
                 Note(title, "in progress", done=f"Claimed by {self.w.name}.", next="Checking readiness."))
        if features.on("readiness_gate"):
            self.check_ready(None)
        withheld, attach_dir = self.attachments()
        spec = ""
        if self.mode == "resume":
            spec = self.w.state.get("spec", self.key) or ""
        elif features.on("triage"):
            spec = self.triage_and_spec()
        self.clone()
        if features.on("readiness_gate"):
            self.check_ready(self.rules)
        self.check_restricted_dependencies()
        return self.implement(spec, withheld, attach_dir)

    def check_ready(self, rules: RepoRules | None) -> None:
        result = readiness.evaluate(self.ticket, self.cfg.limits, self.w.agent.repos, rules)
        if result.ready:
            return
        labels = self.cfg.labels
        self.labels(add=[labels.needs_info], remove=[labels.ready])
        self.w.writes.transition(self.key, LogicalStatus.NEEDS_INFO)
        self.release()
        self.say("bounced", Note("Not ready", "returned to requester", done="The readiness check failed.",
                                 next="Fix the items below, then re-apply the ready label.",
                                 questions=result.failures))
        raise Stop(Outcome.BOUNCED, "; ".join(result.failures))

    def attachments(self) -> tuple[int, str]:
        if not self.ticket.attachments:
            return 0, ""
        withheld = 0
        self.pending_attachments: list[tuple[str, bytes]] = []
        for att in self.ticket.attachments:
            data = self.w.tickets.get_attachment(self.key, att.id)
            if self.guard.check_attachment(att.filename, data).allowed:
                self.pending_attachments.append((att.filename, data))
            else:
                withheld += 1
                self.rec.event("attachment_withheld", {"name": att.filename}, always=True)
        return withheld, "attachments" if self.pending_attachments else ""

    def triage_and_spec(self) -> str:
        self.rec.stage("triage")
        raw = self.ask_model("triage", "Classify the ticket as easy or hard. Reply with JSON "
                             '{"difficulty": "easy"|"hard", "reason": "..."}.', self.ticket_text())
        self.rec.stage_done("triage")
        try:
            verdict = json.loads(raw)
            hard = verdict.get("difficulty") != "easy"
            reason = str(verdict.get("reason", ""))
        except (json.JSONDecodeError, AttributeError):
            hard, reason = True, "triage output was not valid JSON; treating as hard"
        self.say("triaged", Note("Triaged", "in progress", done=f"{'Hard' if hard else 'Easy'}: {reason}",
                                 next="Writing a spec for review." if hard and self.w.features.on("spec") else "Implementing."))
        if not hard or not self.w.features.on("spec"):
            return ""
        self.rec.stage("spec")
        spec = self.ask_model("spec", "Write a product spec (what) and a tech spec (how) in markdown.",
                              self.ticket_text())
        self.rec.stage_done("spec")
        spec = self.guard.filter_output(spec).text
        self.w.state.put("spec", self.key, spec)
        self.w.writes.transition(self.key, LogicalStatus.SPEC_REVIEW)
        self.release()
        self.say("spec_posted", Note("Spec ready for review", "spec review", done=spec,
                                     next=f"Apply the {self.cfg.labels.spec_approved} label to approve."))
        if "notifier" in self.w.c.allowed_ports:
            self.w.c.port("notifier").notify(Notification(NotificationKind.SPEC_REVIEW, self.key,
                                                          tuple(filter(None, [self.reporter])),
                                                          f"Spec ready for {self.key}", spec[:2000]))
        raise Stop(Outcome.SPEC_REVIEW, "spec posted for review")

    def ticket_text(self) -> str:
        return (f"{self.ticket.key}: {self.ticket.summary}\n\n{self.ticket.description}\n\nAcceptance criteria:\n"
                f"{readiness.text_field(self.ticket, 'acceptance_criteria')}")

    def clone(self) -> None:
        repo_path = readiness.text_field(self.ticket, "repo")
        if repo_path not in self.w.agent.repos:
            raise self.blocked(f"Repo {repo_path} is outside this agent's scope.")
        branch = self.cfg.git.branches.get(repo_path, self.cfg.git.default_branch)
        self.repo = RepoRef("", repo_path, branch)
        self.ws = self.w.sandbox.prepare(WorkspaceSpec(self.run_id, self.w.name, self.key))
        self.clone_root = self.ws.root / "clone"
        self.rec.stage("clone")
        try:
            self.checkout = self.w.git.clone(self.repo, self.clone_root, branch)
        except SubmoduleFailure as exc:
            raise self.blocked(f"A submodule could not be checked out: {exc.path}.", [exc.path]) from None
        except NotFound:
            return self.fail_stop(f"Repo {repo_path} could not be cloned.")
        self.rec.stage_done("clone")
        scan = self.guard.scan_workspace(self.clone_root)
        self.rec.event("access_scan", {"matched_paths": list(scan.matched_paths), "action": scan.action}, always=True)
        self.scan_matches = set(scan.matched_paths)
        self.rules = self.load_rules()
        if self.rules.extra_deny_patterns:
            self.guard = self.guard.derive(self.rules.extra_deny_patterns)
            extra = self.guard.scan_workspace(self.clone_root)
            self.scan_matches |= set(extra.matched_paths)
        failed = [s for s in self.checkout.submodules if not s.ok]
        if failed:
            self.rec.event("submodules_skipped", {"paths": [s.path for s in failed]}, always=True)

    def fail_stop(self, summary: str) -> Stop:
        report = self.fail(summary)
        raise Stop(report.outcome, report.detail)

    def load_rules(self) -> RepoRules:
        path = ".factory/rules.toml"
        if not (self.clone_root / path).is_file() or not self.guard.check_path(self.clone_root, path).allowed:
            return RepoRules()
        try:
            return build(RepoRules, tomllib.loads((self.clone_root / path).read_text()))
        except (tomllib.TOMLDecodeError, ConfigError) as exc:
            return self.fail_stop(f"The repo rules file {path} is invalid: {exc}")

    def check_restricted_dependencies(self) -> None:
        text = f"{self.ticket.summary}\n{self.ticket.description}\n{readiness.text_field(self.ticket, 'acceptance_criteria')}"
        tokens = {t.strip("./") for t in PATH_TOKEN.findall(text)}
        restricted = sorted(t for t in tokens if self.guard.text_matches(t) or t in self.scan_matches)
        if restricted:
            raise self.blocked("The work depends on files restricted by the access policy.", restricted)

    def standards(self) -> list[tuple[str, str]]:
        out = []
        for rel in self.rules.standards:
            if (self.clone_root / rel).is_file() and self.guard.check_path(self.clone_root, rel).allowed:
                out.append((rel, self.guard.filter_output((self.clone_root / rel).read_text(errors="replace")).text))
        return out

    def implement(self, spec: str, withheld: int, attach_dir: str) -> RunReport:
        features = self.w.features
        if not features.on("implement"):
            raise Stop(Outcome.BLOCKED, "implement is disabled")
        copy = self.ws.root / "work"
        workspace.isolate(self.clone_root, copy)
        self.guard.scan_workspace(copy)
        factory_dir = self.ws.root / ".factory"
        factory_dir.mkdir(exist_ok=True)
        if attach_dir:
            (self.ws.root / attach_dir).mkdir(exist_ok=True)
            for name, data in self.pending_attachments:
                (self.ws.root / attach_dir / Path(name).name).write_bytes(data)
        stage = self.cfg.models.stages.implement
        harness = self.w.c.harness(stage.harness)
        runtime = SandboxRuntime(self.w.sandbox, self.ws, "work", self.guard, self.cfg.limits.max_denied_access_attempts)
        submodule_paths = [s.path for s in self.checkout.submodules]
        attempt = Attempt()
        standards = self.standards()
        self.say("implementation_started", Note("Implementing", "in progress",
                                                done=f"Harness {stage.harness}, model {stage.provider}/{stage.model}.",
                                                next="Running gates when the change is ready."))
        while True:
            used = int(self.w.state.get("attempts", self.key) or 0)
            if used >= self.cfg.limits.max_attempts:
                reasons = "; ".join(attempt.feedback) or "no successful attempt"
                return self.fail(f"Gave up after {used} attempt(s): {reasons}")
            self.w.state.put("attempts", self.key, str(used + 1))
            task_file = factory_dir / "task.md"
            result_file = factory_dir / "result.json"
            result_file.unlink(missing_ok=True)
            task_file.write_text(instructions.render(self.ticket, self.cfg, self.rules, standards, spec,
                                                     attempt.feedback, withheld,
                                                     str(self.ws.root / attach_dir) if attach_dir else ""))
            denials_before = len(self.guard.denials)
            self.rec.stage("implement")
            gw_cfg = self.cfg.gateway
            with Gateway(self.models, stage.provider, stage.model, self.context("implement"), gw_cfg.bind_host,
                         tuple(gw_cfg.dialects)) as gw:
                h_cfg = self.cfg.harnesses[stage.harness]
                task = HarnessTask(self.run_id, "implement", self.key, Workspace(self.ws.id, copy), task_file,
                                   result_file, ModelEndpoint(gw.base_url, gw.token, stage.model, tuple(gw_cfg.dialects)),
                                   float(h_cfg.timeout_minutes * 60), h_cfg.max_turns)
                result = harness.run(task, runtime)
            self.rec.stage_done("implement")
            denied = self.guard.denials[denials_before:]
            for d in denied:
                self.rec.event("access_denied", {"kind": d.kind, "reason": d.reason}, always=True)
            self.rec.event("harness_result", {"status": result.status.value, "summary": result.summary})
            if gw.budget_exceeded:
                raise BudgetExceeded(gw.budget_exceeded)
            if len(denied) >= self.cfg.limits.max_denied_access_attempts:
                raise self.blocked(f"Stopped after {len(denied)} attempts to read files restricted by the access policy.")
            if result.status is HarnessStatus.NEEDS_INFO:
                raise self.needs_info(result.questions, result.summary)
            if result.status is HarnessStatus.BLOCKED:
                raise self.blocked(result.summary or "The harness reported the task is blocked.")
            if result.status is not HarnessStatus.DONE:
                attempt.feedback = [f"harness {result.status.value}: {result.summary}"]
                continue
            found = workspace.changes(copy)
            stats = workspace.line_stats(copy)
            report = gates.check_changes(found, stats, self.ticket, self.cfg, self.rules, submodule_paths, self.guard,
                                         copy)
            if report.passed:
                report.extend(self.run_checks(runtime))
            self.rec.event("gates", {"failures": report.failures, "flags": report.flags}, always=True)
            if not report.passed:
                attempt.feedback = report.failures
                continue
            return self.publish(found, stats, result.summary, report, runtime, copy)

    def run_checks(self, runtime: SandboxRuntime) -> gates.GateReport:
        report = gates.GateReport()
        commands = self.rules.commands
        timeout = float(self.rules.command_timeout_seconds)
        self.check_logs: list[str] = []
        for phase, argvs in (("setup", commands.setup), ("lint", commands.lint), ("test", commands.test)):
            for argv in argvs:
                res = runtime.run(Command(tuple(argv), ".", {}, timeout))
                self.check_logs.append(f"$ {' '.join(argv)}\nexit {res.exit_code}\n{res.stdout[-4000:]}{res.stderr[-4000:]}")
                if res.exit_code != 0:
                    report.failures.append(f"{phase} failed: {' '.join(argv)} (exit {res.exit_code})")
                    return report
        self.evidence: list[Artifact] = []
        if self.w.features.on("verify"):
            verify = commands.verify or commands.test
            if not verify and not readiness.text_field(self.ticket, "verify_steps"):
                report.failures.append("no verify or test command is defined")
            for argv in verify:
                res = runtime.run(Command(tuple(argv), ".", {}, timeout))
                self.check_logs.append(f"$ {' '.join(argv)}\nexit {res.exit_code}\n{res.stdout[-4000:]}{res.stderr[-4000:]}")
                if res.exit_code != 0:
                    report.failures.append(f"verify failed: {' '.join(argv)} (exit {res.exit_code})")
                    return report
            log_path = self.ws.root / ".factory" / "verify.log"
            log_path.write_text(self.guard.filter_output("\n\n".join(self.check_logs)).text)
            self.evidence = [Artifact("verify.log", log_path, "text/plain")]
            if self.rules.evidence:
                found = self.w.sandbox.collect_artifacts(self.ws, [f"work/{p}" for p in self.rules.evidence])
                found = [a for a in found if self.guard.check_path(self.ws.root / "work",
                                                                   a.name[len("work/"):]).allowed]
                if not found:
                    report.failures.append("no verification evidence was produced (rules.evidence matched nothing)")
                self.evidence += [Artifact(a.name[len("work/"):], a.path, a.media_type) for a in found]
            for artifact in self.evidence:
                self.rec.artifact(artifact)
        return report

    def review(self, diff: str) -> dict:
        if not self.w.features.on("review"):
            return {}
        self.rec.stage("review")
        standards = "\n\n".join(text for _, text in self.standards())
        raw = self.ask_model("review", "Review this change against the standards. Reply with JSON "
                             '{"summary", "blast_radius", "merge_danger": "low"|"medium"|"high", '
                             '"findings": [...], "architecture_diff": "..."}.',
                             f"Standards:\n{standards}\n\nDiff:\n{diff}")
        self.rec.stage_done("review")
        try:
            doc = json.loads(raw)
            return doc if isinstance(doc, dict) else {"summary": raw}
        except json.JSONDecodeError:
            return {"summary": self.guard.filter_output(raw).text}

    def publish(self, found, stats, harness_summary: str, report: gates.GateReport, runtime, copy: Path) -> RunReport:
        diff = self.guard.filter_output(workspace.diff_text(copy)).text
        review = self.review(diff)
        if review:
            self.say("review_done", Note("Review done", "in progress", done=str(review.get("summary", "")),
                                         next="Publishing."))
        if self.w.features.on("verify"):
            self.say("verification_done", Note("Verified", "in progress",
                                               done=f"{len(self.evidence)} evidence artifact(s).", next="Publishing."))
        branch = self.cfg.output.branch_pattern.format(agent=self.w.name, ticket_key=self.key)
        account = self.w.agent.service_account
        git = Git(GitOptions(clone_depth=0, submodules_enabled=False, author_name=account,
                             author_email=f"{account}@factory.invalid", allow_protocols="file"))
        git.run(["checkout", "-q", "-B", branch], cwd=self.clone_root)
        paths = workspace.apply(found, copy, self.clone_root)
        git.run(["add", "-A", "--", *paths], cwd=self.clone_root)
        summary = self.guard.filter_output(harness_summary).text
        git.run(["commit", "-q", "-m", f"{self.key}: {self.ticket.summary}\n\n{summary}"], cwd=self.clone_root)
        description = self.mr_description(found, stats, summary, review, report)
        template = gates.check_template(description)
        if not template.passed:
            return self.fail("; ".join(template.failures))
        self.w.writes.push_branch(self.clone_root, branch)
        links: dict[str, str] = {"Branch": branch}
        mr_url = ""
        if self.cfg.output.mode == "merge_request":
            mr = self.w.writes.open_merge_request(MergeRequestDraft(
                self.repo, branch, self.repo.default_branch, f"{self.key}: {self.ticket.summary}", description,
                f"{self.run_id}:mr"))
            mr_url = mr.url
            links["Merge request"] = mr.url
            self.w.state.put("mr", self.key, json.dumps({"repo": self.repo.path, "id": mr.id, "branch": branch}))
            outcome = Outcome.MR_OPENED
        else:
            compare = self.w.git.compare_url(self.repo, self.repo.default_branch, branch)
            if compare:
                links["Compare"] = compare
            outcome = Outcome.BRANCH_PUSHED
        self.w.writes.transition(self.key, LogicalStatus.IN_REVIEW)
        self.release()
        self.w.state.delete("attempts", self.key)
        done = description if self.cfg.output.mode == "branch_only" else summary
        self.say("finished", Note("Ready for human review", "in review", done=done, links=links,
                                  next="A human reviews and merges. The factory never merges."))
        return RunReport(self.key, outcome, summary, self.run_id, mr_url, branch)

    def mr_description(self, found, stats, summary: str, review: dict, report: gates.GateReport) -> str:
        changed = "\n".join(f"- {c.status} {c.path}" for c in found)
        evidence = "\n".join(f"- {a.name}" for a in getattr(self, "evidence", [])) or "- (verify disabled)"
        flags = "\n".join(f"- {f}" for f in report.flags)
        return "\n\n".join([
            f"## What changed\n{summary or '(no summary)'}\n\n{changed}\n\n+{stats.added} / -{stats.deleted} lines",
            f"## Why\n{self.key}: {self.ticket.summary}",
            f"## Blast radius\n{review.get('blast_radius', 'Not reviewed.')}\n\n"
            f"Merge danger: {review.get('merge_danger', 'unknown')}" + (f"\n\nFlags:\n{flags}" if flags else ""),
            f"## How verified\n{evidence}",
            f"## Architecture diff\n{review.get('architecture_diff', 'Not produced.')}",
        ])
