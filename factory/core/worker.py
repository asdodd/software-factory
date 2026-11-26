"""One agent's pipeline: claim, readiness, triage, spec, clone, isolate,
implement (any harness), gates, verify, review, publish, and trace. The same
implement-and-gate loop also serves resumed tickets (after a human answers),
review fixes on the agent's own MR branch, and /apply follow-ups.

Every external write goes through Writes; every model call goes through
BudgetedModels; every file a harness sees has passed the access policy."""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from factory.config.schema import RepoRules
from factory.config.validate import ConfigError, build
from factory.core import architecture, gates, instructions, quality, readiness, skills, workspace
from factory.core.budget import BudgetedModels
from factory.core.claim import Claimer
from factory.core.container import Container
from factory.core.gateway import Gateway
from factory.core.recorder import Recorder
from factory.core.stateindex import Index, Records
from factory.core.writes import Note, Writes
from factory.ports.errors import BudgetExceeded, NotFound, RateLimited, SubmoduleFailure
from factory.ports.types import (
    Artifact,
    CallContext,
    Command,
    CommandResult,
    HarnessResult,
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
    Suggestion,
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
class FixRequest:
    """Work on the agent's own MR branch: a reviewer change request, or /apply."""

    repo: str
    mr_id: str
    branch: str
    thread_id: str
    request: str
    comment_id: str
    apply: list[dict] = field(default_factory=list)


class Stop(Exception):
    def __init__(self, outcome: Outcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


class GaveUp(Exception):
    pass


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
        self.notifier = container.port("notifier")
        self.metrics = container.metrics
        self.worker_id = worker_id or f"{agent_name}:{self.ids.new_id('worker')}"
        self.dry_run = self.features.on("dry_run")
        self.writes = Writes(self.tickets, self.git, self.state, self.clock, container.redactor, self.cfg,
                             agent_name, self.dry_run)
        self.claimer = Claimer(self.tickets, self.writes, self.clock, self.agent, self.cfg, self.worker_id,
                               self.metrics, self.dry_run)
        self.paused = Records(self.state, f"paused:{agent_name}")
        self.paused_index = Index(self.state, "paused-index", agent_name)
        self.mrs = Records(self.state, f"mr:{agent_name}")
        self.mr_index = Index(self.state, "mr-index", agent_name)
        self.service_accounts = {a.service_account for a in loaded.agents.values()}
        self.tickets.validate_filter(self.claimer.filter)

    # ---- loop ------------------------------------------------------------
    def run_once(self) -> list[RunReport]:
        from factory.core.collaboration import poll_mrs, poll_replies

        reports = poll_replies(self) + poll_mrs(self)
        started = 0
        lost = []
        for ticket, mode in self.claimer.candidates():
            if started >= self.agent.claim.max_in_flight:
                break
            claimed = self.claimer.claim(ticket.key, mode)
            if claimed is None:
                lost.append(RunReport(ticket.key, Outcome.CLAIM_LOST, "not claimed"))
                continue
            started += 1
            reports.append(self.process(claimed, mode))
        return reports + lost

    def notify(self, kind: NotificationKind, ticket: Ticket, subject: str, body: str = "", link: str = "") -> None:
        n = self.cfg.notifications
        names = {NotificationKind.SPEC_REVIEW: n.spec_review, NotificationKind.MR_READY: n.mr_ready,
                 NotificationKind.TICKET_FAILED: n.failed, NotificationKind.TICKET_BLOCKED: n.blocked}[kind]
        recipients = list(names)
        if n.include_reporter and ticket.reporter and ticket.reporter.id not in recipients:
            recipients.append(ticket.reporter.id)
        if not recipients or self.dry_run:
            return
        try:
            self.notifier.notify(Notification(kind, ticket.key, tuple(recipients), subject, body[:2000], link))
        except Exception:  # noqa: BLE001 - a failed alert must not fail the ticket
            log.exception("notification %s for %s failed", kind.value, ticket.key)
            self.metrics.incr("notification_failures")

    def process(self, ticket: Ticket, mode: str, answers: tuple[str, ...] = (), rounds: int = 0,
                fix: FixRequest | None = None) -> RunReport:
        run_id = self.ids.new_id("run")
        recorder = Recorder(self.c.trace_writer(self.features.on("feedback.trace_store")), self.c.redactor, self.clock,
                            self.metrics, self.cfg.trace_store.capture_level, run_id=run_id, agent=self.name,
                            ticket_key=ticket.key, config_hash=self.config_hash)
        recorder.repo = readiness.text_field(ticket, "repo")
        recorder.event("run", {"mode": mode}, always=True)
        holder: list[_Run] = []
        models = BudgetedModels.for_config(self.c.provider, self.accounting, self.clock, self.cfg, recorder,
                                           on_request=lambda: holder[0].heartbeat() if holder else None)
        run = _Run(self, ticket, mode, run_id, recorder, models, answers, rounds, fix)
        holder.append(run)
        try:
            report = run.execute_fix() if fix else run.execute()
        except Stop as stop:
            report = RunReport(ticket.key, stop.outcome, stop.detail, run_id)
        except RateLimited as exc:
            report = run.defer(str(exc))
        except BudgetExceeded as exc:
            report = run.fail(f"Spend cap reached: {exc}")
        except Exception as exc:  # noqa: BLE001 - any failure ends in a failed ticket, never a crash loop
            log.exception("run %s failed", run_id)
            report = run.fail(f"Internal error ({type(exc).__name__}). See the trace for run {run_id}.")
        finally:
            run.cleanup()
        if models.waited_seconds:
            recorder.event("request_pacing", {"waited_seconds": round(models.waited_seconds, 1)}, always=True)
        recorder.event("outcome", {"outcome": report.outcome.value, "detail": report.detail}, always=True)
        recorder.finish(report.outcome)
        self.metrics.incr("runs", outcome=report.outcome.value, agent=self.name)
        return report


class _Run:
    def __init__(self, worker: Worker, ticket: Ticket, mode: str, run_id: str, recorder: Recorder,
                 models: BudgetedModels, answers: tuple[str, ...] = (), rounds: int = 0,
                 fix: FixRequest | None = None) -> None:
        self.w = worker
        self.cfg = worker.cfg
        self.ticket = ticket
        self.key = ticket.key
        self.mode = mode
        self.run_id = run_id
        self.rec = recorder
        self.models = models
        self.answers = answers
        self.rounds = rounds
        self.fix = fix
        self.guard = worker.base_guard.derive(self.cfg.access_policy.deny_patterns)
        self.ws: Workspace | None = None
        self.checkout = None
        self.rules = RepoRules()
        self.reporter = ticket.reporter.id if ticket.reporter else ""
        self.team = str(ticket.fields.get("sponsor_team") or worker.agent.team)
        self.pending_attachments: list[tuple[str, bytes]] = []
        self.evidence: list[Artifact] = []
        self.check_logs: list[str] = []
        self.quality_flags: list[str] = []
        self.leased = fix is None
        self.last_renewal = worker.clock.now()

    # ---- helpers -----------------------------------------------------------
    def say(self, milestone: str, note: Note) -> str | None:
        return self.w.writes.comment(self.key, milestone, note, f"{self.run_id}:{milestone}")

    def context(self, stage: str) -> CallContext:
        return CallContext(self.w.name, self.key, stage, self.reporter or self.w.agent.service_account, self.team)

    def ask_model(self, stage: str, system: str, user: str) -> str:
        sm = getattr(self.cfg.models.stages, stage)
        resp = self.models.complete(sm.provider, sm.model,
                                    (Message(Role.SYSTEM, system), Message(Role.USER, user)), self.context(stage))
        return resp.message.content

    def release(self) -> None:
        self.leased = False
        self.w.claimer.release(self.key)

    def stage_skills(self, stage: str, has_spec: bool):
        """Skills for a model-only stage (spec, review), rendered inline."""
        chosen = skills.select(self.cfg, stage, self.ticket, has_spec, self.w.c.loaded.root)
        if chosen:
            self.rec.event("skills", {"stage": stage, "skills": [s.id for s in chosen]}, always=True)
        return chosen, skills.render(chosen, skills.capabilities(self.cfg), self.cfg.skills.max_chars_per_stage)

    def renew(self) -> None:
        if self.fix is None and not self.w.dry_run:
            now = self.w.clock.now()
            expires = now + timedelta(minutes=self.cfg.limits.lease_minutes)
            self.w.writes.update(self.key, {"lease_owner": self.w.worker_id, "lease_expires": expires.isoformat()})
            self.last_renewal = now

    def heartbeat(self) -> None:
        """Called on every model request and pacing step: keep the lease alive through slow,
        paced runs, renewing at most every third of the lease period."""
        if not self.leased:
            return
        last = getattr(self, "last_renewal", None)
        if last is None or (self.w.clock.now() - last) >= timedelta(minutes=self.cfg.limits.lease_minutes / 3):
            self.renew()

    def labels(self, add=(), remove=()) -> None:
        self.w.writes.set_labels(self.key, add, remove)

    def reply(self, body: str, suffix: str) -> None:
        f = self.fix
        repo = RepoRef("", f.repo, self.cfg.git.branches.get(f.repo, self.cfg.git.default_branch))
        self.w.writes.mr_reply(repo, f.mr_id, f.thread_id, body, f"{f.comment_id}:{suffix}")

    def fail(self, summary: str) -> RunReport:
        labels = self.cfg.labels
        self.w.writes.transition(self.key, LogicalStatus.FAILED)
        self.labels(add=[labels.failed], remove=[labels.ready])
        self.release()
        self.say("failed", Note("Failed", "failed", done=summary,
                                next="A human should review the ticket and the trace before retrying."))
        self.w.notify(NotificationKind.TICKET_FAILED, self.ticket, f"{self.key} failed: {summary[:200]}")
        return RunReport(self.key, Outcome.FAILED, summary, self.run_id)

    def defer(self, why: str) -> RunReport:
        """The hourly request budget is used up: give the ticket back for a later cycle."""
        if self.fix is None:
            self.w.writes.transition(self.key, LogicalStatus.READY)
            self.release()
            self.say("deferred", Note("Deferred", "ready", done=f"Paused: {why}.",
                                      next="The factory will pick this up again when the request budget allows."))
        return RunReport(self.key, Outcome.DEFERRED, why, self.run_id)

    def blocked(self, summary: str, paths: list[str] | None = None) -> Stop:
        detail = summary + ("\nPaths: " + ", ".join(sorted(paths)) if paths else "")
        if self.fix is not None:
            self.reply(f"I could not make this change: {detail}", "blocked")
            return Stop(Outcome.FIX_FAILED, detail)
        self.w.writes.transition(self.key, LogicalStatus.BLOCKED)
        self.release()
        self.say("blocked", Note("Blocked", "blocked", done=detail, next="Needs a human decision."))
        self.w.notify(NotificationKind.TICKET_BLOCKED, self.ticket, f"{self.key} is blocked", detail)
        return Stop(Outcome.BLOCKED, detail)

    def stalled(self, why: str) -> Stop:
        labels = self.cfg.labels
        self.labels(add=[labels.stalled], remove=[labels.needs_info])
        self.w.writes.transition(self.key, LogicalStatus.STALLED)
        self.release()
        self.say("stalled", Note("Stalled", "stalled", done=why, next="A human should pick this up or clarify the ticket."))
        return Stop(Outcome.STALLED, why)

    def needs_info(self, questions: tuple[str, ...], why: str) -> Stop:
        questions = questions or ("Please clarify the ticket.",)
        if self.fix is not None:
            listed = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
            self.reply(f"Before I change this, I need to know:\n{listed}", "question")
            return Stop(Outcome.REPLIED, "; ".join(questions))
        jc = self.cfg.jira_comments
        rounds = self.rounds + 1
        if rounds > jc.max_question_rounds:
            raise self.stalled(f"Still unclear after {jc.max_question_rounds} rounds of questions: {why}")
        if jc.needs_info_action == "status":
            self.w.writes.transition(self.key, LogicalStatus.NEEDS_INFO)
        self.labels(add=[self.cfg.labels.needs_info])
        if jc.lease_while_paused == "release":
            self.release()
        self.say("needs_info", Note("Needs more information", "paused", done=why,
                                    next="Answer the questions in a comment on this ticket.", questions=questions))
        comments = self.w.tickets.list_comments(self.key).items if not self.w.dry_run else ()
        self.w.paused.put(self.key, {
            "run_id": self.run_id, "worker": self.w.worker_id, "questions": list(questions), "rounds": rounds,
            "answers": list(self.answers), "since": self.w.clock.now().isoformat(),
            "cursor": comments[-1].id if comments else None, "last_poll": None,
        })
        self.w.paused_index.add(self.key)
        return Stop(Outcome.NEEDS_INFO, "; ".join(questions))

    def cleanup(self) -> None:
        if self.ws is not None:
            self.w.sandbox.teardown(self.ws)

    # ---- stages ------------------------------------------------------------
    def execute(self) -> RunReport:
        features = self.w.features
        if self.mode == "answer":
            self.say("resumed", Note("Resumed", "in progress", done="Answers received; continuing the work.",
                                     next="Implementing with the answers."))
        else:
            title = "Reclaimed after lease expiry" if self.mode == "reclaim" else "Claimed"
            self.say("reclaimed" if self.mode == "reclaim" else "claimed",
                     Note(title, "in progress", done=f"Claimed by {self.w.name}.", next="Checking readiness."))
        if features.on("readiness_gate"):
            self.check_ready(None)
        withheld, attach_dir = self.attachments()
        spec = ""
        if self.mode in ("resume", "answer"):
            spec = self.w.state.get("spec", self.key) or ""
        elif features.on("triage"):
            spec = self.triage_and_spec()
        self.clone()
        if features.on("readiness_gate"):
            self.check_ready(self.rules)
        self.check_restricted_dependencies()
        if not features.on("implement"):
            raise Stop(Outcome.BLOCKED, "implement is disabled")
        self.say("implementation_started", Note(
            "Implementing", "in progress",
            done=f"Harness {self.cfg.models.stages.implement.harness}, model "
                 f"{self.cfg.models.stages.implement.provider}/{self.cfg.models.stages.implement.model}.",
            next="Running gates when the change is ready."))
        try:
            found, stats, summary, report, copy = self.implement(spec, withheld, attach_dir, "attempts", self.key,
                                                                 self.run_harness)
        except GaveUp as exc:
            return self.fail(str(exc))
        return self.publish(found, stats, summary, report, copy)

    def execute_fix(self) -> RunReport:
        f = self.fix
        self.clone(ref=f.branch)
        if f.apply:
            produce, feedback = self.apply_suggestions, []
        else:
            produce, feedback = self.run_harness, [f"A reviewer asked on the merge request: {f.request}"]
        try:
            found, stats, summary, report, copy = self.implement(
                self.w.state.get("spec", self.key) or "", 0, "", "fix-attempts", f"{f.mr_id}:{f.comment_id}",
                produce, feedback, with_skills=not f.apply)
        except GaveUp as exc:
            self.reply(f"I could not produce a change that passes the gates: {exc}", "gave-up")
            return RunReport(self.key, Outcome.FIX_FAILED, str(exc), self.run_id)
        return self.publish_fix(found, stats, summary, copy)

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
        withheld = 0
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
        _, spec_skills = self.stage_skills("spec", has_spec=False)
        system = "Write a product spec (what) and a tech spec (how) in markdown."
        spec = self.ask_model("spec", f"{system}\n\n{spec_skills}" if spec_skills else system, self.ticket_text())
        self.rec.stage_done("spec")
        spec = self.guard.filter_output(spec).text
        self.w.state.put("spec", self.key, spec)
        self.w.writes.transition(self.key, LogicalStatus.SPEC_REVIEW)
        self.release()
        self.say("spec_posted", Note("Spec ready for review", "spec review", done=spec,
                                     next=f"Apply the {self.cfg.labels.spec_approved} label to approve."))
        self.w.notify(NotificationKind.SPEC_REVIEW, self.ticket, f"Spec ready for review on {self.key}", spec)
        raise Stop(Outcome.SPEC_REVIEW, "spec posted for review")

    def ticket_text(self) -> str:
        return (f"{self.ticket.key}: {self.ticket.summary}\n\n{self.ticket.description}\n\nAcceptance criteria:\n"
                f"{readiness.text_field(self.ticket, 'acceptance_criteria')}")

    def clone(self, ref: str | None = None) -> None:
        repo_path = self.fix.repo if self.fix else readiness.text_field(self.ticket, "repo")
        if repo_path not in self.w.agent.repos:
            raise self.blocked(f"Repo {repo_path} is outside this agent's scope.")
        base = self.cfg.git.branches.get(repo_path, self.cfg.git.default_branch)
        self.repo = RepoRef("", repo_path, base)
        self.ws = self.w.sandbox.prepare(WorkspaceSpec(self.run_id, self.w.name, self.key))
        self.clone_root = self.ws.root / "clone"
        self.rec.stage("clone")
        try:
            self.checkout = self.w.git.clone(self.repo, self.clone_root, ref or base)
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

    # ---- the implement-and-gate loop -----------------------------------------
    def implement(self, spec: str, withheld: int, attach_dir: str, attempts_ns: str, attempts_key: str,
                  produce: Callable[[Path, Path, Path, SandboxRuntime], HarnessResult],
                  feedback: list[str] | None = None, with_skills: bool = True):
        copy = self.ws.root / "work"
        workspace.isolate(self.clone_root, copy)
        self.guard.scan_workspace(copy)
        factory_dir = self.ws.root / ".factory"
        factory_dir.mkdir(exist_ok=True)
        if attach_dir:
            (self.ws.root / attach_dir).mkdir(exist_ok=True)
            for name, data in self.pending_attachments:
                (self.ws.root / attach_dir / Path(name).name).write_bytes(data)
        runtime = SandboxRuntime(self.w.sandbox, self.ws, "work", self.guard, self.cfg.limits.max_denied_access_attempts)
        submodule_paths = [s.path for s in self.checkout.submodules]
        standards = self.standards()
        feedback = list(feedback or [])
        initial = list(feedback)
        answers = tuple(self.guard.filter_output(a).text for a in self.answers)
        rules_text, skills_text, artifacts = self.prepare_delivery(bool(spec)) if with_skills else (None, "", None)
        while True:
            used = int(self.w.state.get(attempts_ns, attempts_key) or 0)
            if used >= self.cfg.limits.max_attempts:
                reasons = "; ".join(f for f in feedback if f not in initial) or "no successful attempt"
                self.w.state.delete(attempts_ns, attempts_key)
                raise GaveUp(f"Gave up after {used} attempt(s): {reasons}")
            self.w.state.put(attempts_ns, attempts_key, str(used + 1))
            self.renew()
            task_file = factory_dir / "task.md"
            result_file = factory_dir / "result.json"
            result_file.unlink(missing_ok=True)
            task_file.write_text(instructions.render(self.ticket, self.cfg, self.rules, standards, spec, feedback,
                                                     withheld, str(self.ws.root / attach_dir) if attach_dir else "",
                                                     answers, skills_text, rules_text))
            denials_before = len(self.guard.denials)
            self.rec.stage("implement")
            result = produce(copy, task_file, result_file, runtime)
            self.rec.stage_done("implement")
            self.collect_artifacts(artifacts)
            denied = self.guard.denials[denials_before:]
            for d in denied:
                self.rec.event("access_denied", {"kind": d.kind, "reason": d.reason}, always=True)
            self.rec.event("harness_result", {"status": result.status.value, "summary": result.summary})
            if len(denied) >= self.cfg.limits.max_denied_access_attempts:
                raise self.blocked(f"Stopped after {len(denied)} attempts to read files restricted by the access policy.")
            if result.status is HarnessStatus.NEEDS_INFO:
                raise self.needs_info(result.questions, result.summary)
            if result.status is HarnessStatus.BLOCKED:
                raise self.blocked(result.summary or "The harness reported the task is blocked.")
            if result.status is not HarnessStatus.DONE:
                feedback = initial + [f"harness {result.status.value}: {result.summary}"]
                continue
            found = workspace.changes(copy)
            stats = workspace.line_stats(copy)
            report = gates.check_changes(found, stats, self.ticket, self.cfg, self.rules, submodule_paths, self.guard,
                                         copy)
            if report.passed:
                report.extend(self.run_checks(runtime, copy, [c.path for c in found]))
            self.rec.event("gates", {"failures": report.failures, "flags": report.flags}, always=True)
            if not report.passed:
                feedback = initial + report.failures
                continue
            self.w.state.delete(attempts_ns, attempts_key)
            return found, stats, result.summary, report, copy

    def prepare_delivery(self, has_spec: bool) -> tuple[str | None, str, Path | None]:
        """Rules and implement-stage skills for the CLI agent, delivered its own way.

        Returns (rules for the prompt, skills section for the prompt, working-notes dir).
        In native mode the rules also go to the agent's global memory file and the
        skills to its skills folder, both in the sandbox home (outside the repository
        copy); the prompt then names the skills to load. Agents without those
        mechanisms, and inline mode, get everything in the prompt."""
        caps = skills.capabilities(self.cfg)
        stage = self.cfg.models.stages.implement
        harness = self.w.c.harness(stage.harness)
        scripted = self.cfg.harnesses[stage.harness].adapter == "harness.scripted"
        artifacts = None if scripted else self.ws.root / ".factory" / "artifacts"
        if artifacts is not None:
            artifacts.mkdir(parents=True, exist_ok=True)
        notes = str(artifacts) if artifacts else ""
        native = self.cfg.skills.delivery == "native"
        rules = skills.base_rules(caps)
        memory = getattr(harness, "native_memory_file", "")
        if native and memory:
            skills.install_rules(rules, self.ws.root, memory)
        chosen = skills.select(self.cfg, "implement", self.ticket, has_spec, self.w.c.loaded.root)
        if chosen:
            self.rec.event("skills", {"stage": "implement", "skills": [s.id for s in chosen],
                                      "delivery": self.cfg.skills.delivery}, always=True)
        dirs = tuple(getattr(harness, "native_skill_dirs", ()))
        if chosen and native and dirs:
            installed = skills.install(chosen, caps, self.ws.root, dirs)
            text = skills.pointer(chosen, caps, notes, installed)
        else:
            text = skills.render(chosen, caps, self.cfg.skills.max_chars_per_stage, notes)
        return rules, text, artifacts

    def collect_artifacts(self, artifacts: Path | None) -> None:
        if artifacts is None or not artifacts.is_dir():
            return
        for path in sorted(artifacts.rglob("*")):
            if path.is_file() and not path.is_symlink() and not self.guard.content_matches(path.read_bytes()):
                self.rec.artifact(Artifact(f"notes/{path.relative_to(artifacts)}", path, "text/markdown"))

    def run_harness(self, copy: Path, task_file: Path, result_file: Path, runtime: SandboxRuntime) -> HarnessResult:
        stage = self.cfg.models.stages.implement
        harness = self.w.c.harness(stage.harness)
        gw_cfg = self.cfg.gateway
        h_cfg = self.cfg.harnesses[stage.harness]
        with Gateway(self.models, stage.provider, stage.model, self.context("implement"), gw_cfg.bind_host,
                     tuple(gw_cfg.dialects)) as gw:
            task = HarnessTask(self.run_id, "implement", self.key, Workspace(self.ws.id, copy), task_file, result_file,
                               ModelEndpoint(gw.base_url, gw.token, stage.model, tuple(gw_cfg.dialects)),
                               float(h_cfg.timeout_minutes * 60), h_cfg.max_turns, skills.capabilities(self.cfg))
            result = harness.run(task, runtime)
        if gw.budget_exceeded:
            raise BudgetExceeded(gw.budget_exceeded)
        return result

    def apply_suggestions(self, copy: Path, task_file: Path, result_file: Path, runtime) -> HarnessResult:
        """Deterministic: apply each suggested replacement, bottom-up per file so line numbers stay valid."""
        applied = 0
        by_file: dict[str, list[dict]] = {}
        for s in self.fix.apply:
            by_file.setdefault(s["path"], []).append(s)
        for path, items in by_file.items():
            if not self.guard.check_path(copy, path).allowed or not (copy / path).is_file():
                continue
            lines = (copy / path).read_text().splitlines(keepends=True)
            for s in sorted(items, key=lambda s: s["line_start"], reverse=True):
                start, end = s["line_start"] - 1, s["line_end"]
                if 0 <= start < end <= len(lines):
                    replacement = s["replacement"] if s["replacement"].endswith("\n") else s["replacement"] + "\n"
                    lines[start:end] = [replacement]
                    applied += 1
            (copy / path).write_text("".join(lines))
        if not applied:
            return HarnessResult(HarnessStatus.BLOCKED, "none of the suggestions could be applied")
        return HarnessResult(HarnessStatus.DONE, f"Applied {applied} suggestion(s).")

    def run_checks(self, runtime: SandboxRuntime, copy: Path, changed: list[str]) -> gates.GateReport:
        report = gates.GateReport()
        commands = self.rules.commands
        timeout = float(self.rules.command_timeout_seconds)
        self.check_logs = []

        def run(phase: str, argv: list[str]) -> bool:
            res = runtime.run(Command(tuple(argv), ".", {}, timeout))
            self.check_logs.append(f"$ {' '.join(argv)}\nexit {res.exit_code}\n{res.stdout[-4000:]}{res.stderr[-4000:]}")
            if res.exit_code != 0:
                report.failures.append(f"{phase} failed: {' '.join(argv)} (exit {res.exit_code})")
            return res.exit_code == 0

        for phase, argvs in (("setup", commands.setup), ("lint", commands.lint), ("test", commands.test)):
            for argv in argvs:
                if not run(phase, argv):
                    return report
        report.extend(self.quality(runtime, copy, changed, run))
        if not report.passed:
            return report
        self.evidence = []
        if self.w.features.on("verify"):
            verify = commands.verify or commands.test
            if not verify and not readiness.text_field(self.ticket, "verify_steps"):
                report.failures.append("no verify or test command is defined")
            for argv in verify:
                if not run("verify", argv):
                    return report
            log_path = self.ws.root / ".factory" / "verify.log"
            log_path.write_text(self.guard.filter_output("\n\n".join(self.check_logs)).text)
            self.evidence = [Artifact("verify.log", log_path, "text/plain")]
            if self.rules.evidence:
                found = self.w.sandbox.collect_artifacts(self.ws, [f"work/{p}" for p in self.rules.evidence])
                found = [a for a in found if self.guard.check_path(copy, a.name[len("work/"):]).allowed]
                if not found:
                    report.failures.append("no verification evidence was produced (rules.evidence matched nothing)")
                self.evidence += [Artifact(a.name[len("work/"):], a.path, a.media_type) for a in found]
            for artifact in self.evidence:
                self.rec.artifact(artifact)
        return report

    def quality(self, runtime, copy: Path, changed: list[str], run) -> gates.GateReport:
        report = gates.GateReport()
        g = self.cfg.gates
        if self.rules.coverage_report:
            path = copy / self.rules.coverage_report
            if not path.is_file():
                report.failures.append(f"coverage report {self.rules.coverage_report} was not produced by the tests")
            else:
                added = quality.added_lines(workspace.diff_text(copy, context=0))
                covered, instrumented = quality.changed_line_coverage(quality.load_coverage(path), added, copy)
                if instrumented:
                    ratio = covered / instrumented
                    line = f"coverage on changed lines {covered}/{instrumented} ({ratio:.0%})"
                    if ratio < g.min_changed_line_coverage:
                        report.failures.append(f"{line} is below {g.min_changed_line_coverage:.0%}")
                    else:
                        report.flags.append(line)
        else:
            report.flags.append("coverage on changed lines not measured (no coverage_report in the repo rules)")
        m = self.rules.mutation
        if m.command and report.passed:
            if not run("mutation", m.command):
                return report
            path = copy / m.report
            if not path.is_file():
                report.failures.append(f"mutation report {m.report} was not produced")
                return report
            score, mutants = quality.mutation_score(path, m.format, set(changed), copy)
            if score is None:
                report.flags.append("mutation testing found no mutants in the changed files")
            elif score < g.min_mutation_score:
                report.failures.append(f"mutation score {score:.0%} on changed code is below {g.min_mutation_score:.0%}"
                                       " (tests may be tautological)")
            else:
                report.flags.append(f"mutation score {score:.0%} over {mutants} mutant(s)")
        elif not m.command:
            report.flags.append("mutation score not measured (no mutation command in the repo rules)")
        self.quality_flags = report.flags
        return report

    # ---- review and publish ----------------------------------------------------
    def review(self, diff: str) -> dict:
        if not self.w.features.on("review"):
            return {}
        self.rec.stage("review")
        standards = "\n\n".join(text for _, text in self.standards())
        spec = self.w.state.get("spec", self.key) or ""
        _, review_skills = self.stage_skills("review", has_spec=bool(spec))
        system = ("You are reviewing a change you did not write. Check it against the standards and against the "
                  "ticket (and spec, if any). Reply with JSON "
                  '{"summary": "...", "blast_radius": "...", "merge_danger": "low"|"medium"|"high", '
                  '"findings": [{"note": "...", "path": "...", "line_start": n, "line_end": n, "replacement": "..."}], '
                  '"architecture_diff": "..."}. Give path, lines, and replacement only for concrete suggested changes.')
        raw = self.ask_model(
            "review", f"{system}\n\n{review_skills}" if review_skills else system,
            f"Ticket:\n{self.ticket_text()}\n\n" + (f"Approved spec:\n{spec}\n\n" if spec else "")
            + f"Standards:\n{standards}\n\nDiff:\n{diff}")
        self.rec.stage_done("review")
        try:
            doc = json.loads(raw)
            return doc if isinstance(doc, dict) else {"summary": raw}
        except json.JSONDecodeError:
            return {"summary": self.guard.filter_output(raw).text}

    def commit(self, branch: str, found, copy: Path, message: str) -> None:
        account = self.w.agent.service_account
        git = Git(GitOptions(clone_depth=0, submodules_enabled=False, author_name=account,
                             author_email=f"{account}@factory.invalid", allow_protocols="file"))
        git.run(["checkout", "-q", "-B", branch], cwd=self.clone_root)
        paths = workspace.apply(found, copy, self.clone_root)
        workspace.stage_all(git, self.clone_root, paths)
        git.run(["commit", "-q", "-m", message], cwd=self.clone_root)
        self.commit_sha = git.head(self.clone_root)

    def publish(self, found, stats, harness_summary: str, report: gates.GateReport, copy: Path) -> RunReport:
        diff = self.guard.filter_output(workspace.diff_text(copy)).text
        review = self.review(diff)
        if review:
            self.say("review_done", Note("Review done", "in progress", done=str(review.get("summary", "")),
                                         next="Publishing."))
        if self.w.features.on("verify"):
            self.say("verification_done", Note("Verified", "in progress",
                                               done=f"{len(self.evidence)} evidence artifact(s).", next="Publishing."))
        branch = self.cfg.output.branch_pattern.format(agent=self.w.name, ticket_key=self.key)
        summary = self.guard.filter_output(harness_summary).text
        arch = architecture.diagram([(c.status, c.path) for c in found], copy, workspace.tracked_files(copy))
        self.renew()
        self.commit(branch, found, copy, f"{self.key}: {self.ticket.summary}\n\n{summary}")
        description = self.mr_description(found, stats, summary, review, report, arch)
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
            suggestions = self.post_suggestions(mr.id, review)
            if self.w.features.on("mr.read_comments") and not self.w.dry_run:
                self.w.mrs.put(self.key, {"repo": self.repo.path, "id": mr.id, "branch": branch, "cursor": None,
                                          "run_id": self.run_id, "run_started": self.rec.started.isoformat(),
                                          "last_poll": None, "last_activity": self.w.clock.now().isoformat(),
                                          "fix_rounds": 0, "suggestions": suggestions, "applies": 0})
                self.w.mr_index.add(self.key)
            outcome = Outcome.MR_OPENED
        else:
            compare = self.w.git.compare_url(self.repo, self.repo.default_branch, branch)
            if compare:
                links["Compare"] = compare
            outcome = Outcome.BRANCH_PUSHED
        self.w.writes.transition(self.key, LogicalStatus.IN_REVIEW)
        self.release()
        done = description if self.cfg.output.mode == "branch_only" else summary
        self.say("finished", Note("Ready for human review", "in review", done=done, links=links,
                                  next="A human reviews and merges. The factory never merges."))
        self.w.notify(NotificationKind.MR_READY, self.ticket, f"{self.key} is ready for review", summary,
                      mr_url or links.get("Compare", branch))
        return RunReport(self.key, outcome, summary, self.run_id, mr_url, branch)

    def post_suggestions(self, mr_id: str, review: dict) -> list[dict]:
        posted = []
        for i, f in enumerate(review.get("findings") or []):
            if not isinstance(f, dict) or not all(k in f for k in ("path", "line_start", "line_end", "replacement")):
                continue
            if not self.guard.check_diff_paths([str(f["path"])]).allowed:
                continue
            s = Suggestion(str(f["path"]), int(f["line_start"]), int(f["line_end"]),
                           self.guard.filter_output(str(f["replacement"])).text, str(f.get("note", "")))
            self.w.writes.mr_suggestion(self.repo, mr_id, s, f"{self.run_id}:suggestion:{i}")
            posted.append({"path": s.path, "line_start": s.line_start, "line_end": s.line_end,
                           "replacement": s.replacement, "note": s.note})
        return posted

    def publish_fix(self, found, stats, summary: str, copy: Path) -> RunReport:
        f = self.fix
        summary = self.guard.filter_output(summary).text
        if f.apply:
            info = self.w.mrs.get(self.key) or {}
            n = int(info.get("applies", 0)) + 1
            branch = f"{f.branch}-apply-{n}"
            self.commit(branch, found, copy, f"{self.key}: apply review suggestions\n\n{summary}")
            self.w.writes.push_branch(self.clone_root, branch)
            mr = self.w.writes.open_merge_request(MergeRequestDraft(
                self.repo, branch, f.branch, f"{self.key}: apply review suggestions",
                self.apply_description(found, stats, summary), f"{f.comment_id}:apply"))
            self.reply(f"Opened {mr.url} with the remaining suggestions applied.", "applied")
            return RunReport(self.key, Outcome.FIX_PUSHED, summary, self.run_id, mr.url, branch)
        self.commit(f.branch, found, copy, f"{self.key}: address review feedback\n\n{summary}")
        sha = self.w.writes.push_branch(self.clone_root, f.branch)
        changed = ", ".join(c.path for c in found)
        self.reply(f"Pushed {sha[:12]} to {f.branch}: {summary}\nChanged: {changed}", "fixed")
        return RunReport(self.key, Outcome.FIX_PUSHED, summary, self.run_id, "", f.branch)

    def apply_description(self, found, stats, summary: str) -> str:
        changed = "\n".join(f"- {c.status} {c.path}" for c in found)
        return "\n\n".join([f"## What changed\n{summary}\n\n{changed}\n\n+{stats.added} / -{stats.deleted} lines",
                            f"## Why\nReview suggestions on !{self.fix.mr_id} ({self.key})",
                            "## Blast radius\nLimited to the suggested lines.",
                            "## How verified\nSame gates as the original change.",
                            "## Architecture diff\nNo structural change."])

    def mr_description(self, found, stats, summary: str, review: dict, report: gates.GateReport, arch: str) -> str:
        changed = "\n".join(f"- {c.status} {c.path}" for c in found)
        evidence = "\n".join(f"- {a.name}" for a in self.evidence) or "- (verify disabled)"
        flags = "\n".join(f"- {f}" for f in report.flags)
        findings = "\n".join(f"- {f.get('note', f)}" if isinstance(f, dict) else f"- {f}"
                             for f in review.get("findings") or [])
        reviewer_arch = review.get("architecture_diff", "")
        return "\n\n".join([
            f"## What changed\n{summary or '(no summary)'}\n\n{changed}\n\n+{stats.added} / -{stats.deleted} lines",
            f"## Why\n{self.key}: {self.ticket.summary}",
            f"## Blast radius\n{review.get('blast_radius', 'Not reviewed.')}\n\n"
            f"Merge danger: {review.get('merge_danger', 'unknown')}"
            + (f"\n\nReview findings:\n{findings}" if findings else "") + (f"\n\nGate notes:\n{flags}" if flags else ""),
            f"## How verified\n{evidence}",
            f"## Architecture diff\n```plantuml\n{arch}\n```" + (f"\n\n{reviewer_arch}" if reviewer_arch else ""),
        ])
