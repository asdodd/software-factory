"""One agent's pipeline: claim, readiness, triage, spec, clone, isolate,
implement (any harness), gates, verify, review, publish, and trace. The same
implement-and-gate loop also serves resumed tickets (after a human answers),
review fixes on the agent's own MR branch, and /apply follow-ups.

A ticket may name several repos. Each is cloned, copied into the workspace
(one directory per repo), gated and checked on its own, and every repo that
changed gets its own branch and merge request.

Every external write goes through Writes; every model call goes through
BudgetedModels. Restricted files are the agents' job: the workspace carries the
agent kit (restricted-files skill and hooks), and a run whose agent changed
the kit is stopped."""

from __future__ import annotations

import json
import logging
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from factory import agent_kit
from factory.config.schema import RepoRules
from factory.config.validate import ConfigError, build
from factory.core import architecture, gates, instructions, quality, readiness, workspace
from factory.core.budget import BudgetedModels
from factory.core.claim import Claimer
from factory.core.container import Container
from factory.core.gateway import Gateway
from factory.core.recorder import Recorder
from factory.core.stateindex import Index, Records
from factory.core.writes import Note, Writes
from factory.ports.errors import BudgetExceeded, NotFound, SubmoduleFailure
from factory.ports.types import (
    Artifact,
    CallContext,
    CheckoutResult,
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
    record: str = ""  # the MR record key, when it is not the ticket key (multi-repo tickets)


@dataclass
class RepoWork:
    """One repo of a run: its clone, its rules, and its isolated copy.

    dir is the repo's directory in a multi-repo workspace ("" with one repo,
    where the copy is the harness's working directory itself)."""

    ref: RepoRef
    dir: str
    clone_root: Path
    checkout: CheckoutResult
    rules: RepoRules
    copy: Path | None = None
    found: list = field(default_factory=list)
    stats: workspace.LineStats | None = None

    @property
    def subdir(self) -> str:
        return f"work/{self.dir}" if self.dir else "work"

    def shown(self, path: str) -> str:
        return f"{self.dir}/{path}" if self.dir else path


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
        recorder.repo = ",".join(readiness.ticket_repos(ticket))
        recorder.event("run", {"mode": mode}, always=True)
        models = BudgetedModels(self.c.provider, self.accounting, self.clock, self.cfg.limits.max_tokens_per_ticket,
                                recorder)
        run = _Run(self, ticket, mode, run_id, recorder, models, answers, rounds, fix)
        try:
            report = run.execute_fix() if fix else run.execute()
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
        self.ws: Workspace | None = None
        self.repos: list[RepoWork] = []
        self.checkout = None
        self.rules = RepoRules()
        self.reporter = ticket.reporter.id if ticket.reporter else ""
        self.team = str(ticket.fields.get("sponsor_team") or worker.agent.team)
        self.pending_attachments: list[tuple[str, bytes]] = []
        self.evidence: list[Artifact] = []
        self.check_logs: list[str] = []
        self.quality_flags: list[str] = []

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
        self.w.claimer.release(self.key)

    def renew(self) -> None:
        if self.fix is None and not self.w.dry_run:
            expires = self.w.clock.now() + timedelta(minutes=self.cfg.limits.lease_minutes)
            self.w.writes.update(self.key, {"lease_owner": self.w.worker_id, "lease_expires": expires.isoformat()})

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
        if not features.on("implement"):
            raise Stop(Outcome.BLOCKED, "implement is disabled")
        self.say("implementation_started", Note(
            "Implementing", "in progress",
            done=f"Harness {self.cfg.models.stages.implement.harness}, model "
                 f"{self.cfg.models.stages.implement.provider}/{self.cfg.models.stages.implement.model}.",
            next="Running gates when the change is ready."))
        try:
                summary, report = self.implement(spec, withheld, attach_dir, "attempts", self.key, self.run_harness)
        except GaveUp as exc:
            return self.fail(str(exc))
        return self.publish(summary, report)

    def execute_fix(self) -> RunReport:
        f = self.fix
        self.clone(ref=f.branch)
        if f.apply:
            produce, feedback = self.apply_suggestions, []
        else:
            produce, feedback = self.run_harness, [f"A reviewer asked on the merge request: {f.request}"]
        try:
            summary, _report = self.implement(
                self.w.state.get("spec", self.key) or "", 0, "", "fix-attempts", f"{f.mr_id}:{f.comment_id}",
                produce, feedback)
        except GaveUp as exc:
            self.reply(f"I could not produce a change that passes the gates: {exc}", "gave-up")
            return RunReport(self.key, Outcome.FIX_FAILED, str(exc), self.run_id)
        r = self.repos[0]
        return self.publish_fix(r.found, r.stats, summary, r.copy)

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
        for att in self.ticket.attachments:
            self.pending_attachments.append((att.filename, self.w.tickets.get_attachment(self.key, att.id)))
        return 0, "attachments" if self.pending_attachments else ""

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
        paths = [self.fix.repo] if self.fix else readiness.ticket_repos(self.ticket)
        if not paths:
            raise self.blocked("The ticket names no repo.")
        for repo_path in paths:
            if repo_path not in self.w.agent.repos:
                raise self.blocked(f"Repo {repo_path} is outside this agent's scope.")
        self.ws = self.w.sandbox.prepare(WorkspaceSpec(self.run_id, self.w.name, self.key))
        dirs = workspace.repo_dirs(paths) if len(paths) > 1 else {paths[0]: ""}
        self.rec.stage("clone")
        for repo_path in paths:
            base = self.cfg.git.branches.get(repo_path, self.cfg.git.default_branch)
            repo = RepoRef("", repo_path, base)
            clone_root = self.ws.root / "clones" / (dirs[repo_path] or "repo")
            try:
                checkout = self.w.git.clone(repo, clone_root, ref or base)
            except SubmoduleFailure as exc:
                where = f" of {repo_path}" if len(paths) > 1 else ""
                raise self.blocked(f"A submodule{where} could not be checked out: {exc.path}.", [exc.path]) from None
            except NotFound:
                return self.fail_stop(f"Repo {repo_path} could not be cloned.")
            rules = self.load_rules(clone_root)
            failed = [s for s in checkout.submodules if not s.ok]
            if failed:
                self.rec.event("submodules_skipped", {"repo": repo_path, "paths": [s.path for s in failed]},
                               always=True)
            self.repos.append(RepoWork(repo, dirs[repo_path], clone_root, checkout, rules))
        self.rec.stage_done("clone")
        primary = self.repos[0]
        self.repo, self.clone_root, self.checkout, self.rules = (primary.ref, primary.clone_root, primary.checkout,
                                                                 primary.rules)

    def fail_stop(self, summary: str) -> Stop:
        report = self.fail(summary)
        raise Stop(report.outcome, report.detail)

    def load_rules(self, clone_root: Path) -> RepoRules:
        path = ".factory/rules.toml"
        if not (clone_root / path).is_file():
            return RepoRules()
        try:
            return build(RepoRules, tomllib.loads((clone_root / path).read_text()))
        except (tomllib.TOMLDecodeError, ConfigError) as exc:
            return self.fail_stop(f"The repo rules file {path} is invalid: {exc}")

    def standards(self) -> list[tuple[str, str]]:
        """(name, text) of each repo's standards files, for the reviewer."""
        out = []
        for r in self.repos:
            for rel in r.rules.standards:
                if (r.clone_root / rel).is_file():
                    out.append((r.shown(rel), (r.clone_root / rel).read_text(errors="replace")))
        return out

    def standards_paths(self) -> list[str]:
        """Standards files as paths in the harness's working directory. The
        agent reads them itself, through its own tools and hooks."""
        return [r.shown(rel) for r in self.repos for rel in r.rules.standards if (r.clone_root / rel).is_file()]

    # ---- the implement-and-gate loop -----------------------------------------
    def isolate(self) -> tuple[Path, dict[str, str]]:
        """Copy every repo into ws/work (history replaced by a baseline commit) and
        install the agent kit: inside the copy's baseline with one repo, at the
        top of ws/work with several. Returns the work root and the kit digests."""
        work = self.ws.root / "work"
        if work.exists():
            shutil.rmtree(work)
        digests: dict[str, str] = {}
        if len(self.repos) == 1:
            r = self.repos[0]
            r.copy = work
            workspace.isolate(r.clone_root, work, lambda dest: digests.update(agent_kit.install(dest)))
        else:
            for r in self.repos:
                r.copy = work / r.dir
                workspace.isolate(r.clone_root, r.copy)
            digests = agent_kit.install(work)
        return work, digests

    def implement(self, spec: str, withheld: int, attach_dir: str, attempts_ns: str, attempts_key: str,
                  produce: Callable[[Path, Path, Path, SandboxRuntime], HarnessResult],
                  feedback: list[str] | None = None) -> tuple[str, gates.GateReport]:
        work, kit = self.isolate()
        factory_dir = self.ws.root / ".factory"
        factory_dir.mkdir(exist_ok=True)
        if attach_dir:
            (self.ws.root / attach_dir).mkdir(exist_ok=True)
            for name, data in self.pending_attachments:
                (self.ws.root / attach_dir / Path(name).name).write_bytes(data)
        runtime = SandboxRuntime(self.w.sandbox, self.ws, "work")
        feedback = list(feedback or [])
        initial = list(feedback)
        multi = len(self.repos) > 1
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
            repos = [(r.ref.path, r.dir, r.rules) for r in self.repos] if multi else []
            task_file.write_text(instructions.render(self.ticket, self.cfg, self.rules, self.standards_paths(), spec,
                                                     feedback, withheld,
                                                     str(self.ws.root / attach_dir) if attach_dir else "",
                                                     self.answers, repos))
            self.rec.stage("implement")
            result = produce(work, task_file, result_file, runtime)
            self.rec.stage_done("implement")
            self.rec.event("harness_result", {"status": result.status.value, "summary": result.summary})
            tampered = agent_kit.verify(work, kit)
            if tampered:
                self.rec.event("agent_kit_changed", {"paths": tampered}, always=True)
                raise self.blocked("The agent changed its restricted-files kit, so the run was stopped.", tampered)
            if result.status is HarnessStatus.NEEDS_INFO:
                raise self.needs_info(result.questions, result.summary)
            if result.status is HarnessStatus.BLOCKED:
                raise self.blocked(result.summary or "The harness reported the task is blocked.")
            if result.status is not HarnessStatus.DONE:
                feedback = initial + [f"harness {result.status.value}: {result.summary}"]
                continue
            report = self.gate_all()
            self.rec.event("gates", {"failures": report.failures, "flags": report.flags}, always=True)
            if not report.passed:
                feedback = initial + report.failures
                continue
            self.w.state.delete(attempts_ns, attempts_key)
            return result.summary, report

    def gate_all(self) -> gates.GateReport:
        """Mechanical gates per repo, the size limits over the whole ticket, then
        each changed repo's own lint, test, quality, and verify commands."""
        report = gates.GateReport()
        multi = len(self.repos) > 1
        files = lines = 0
        for r in self.repos:
            r.found = workspace.changes(r.copy)
            r.stats = workspace.line_stats(r.copy)
            if not r.found:
                continue
            files += len(r.found)
            lines += r.stats.added + r.stats.deleted
            one = gates.check_changes(r.found, r.stats, self.ticket, self.cfg, r.rules,
                                      [s.path for s in r.checkout.submodules], size=False)
            report.extend(_prefixed(one, r.ref.path if multi else ""))
        changed = [r for r in self.repos if r.found]
        if not changed:
            report.failures.append("no changes were produced")
            return report
        report.extend(gates.check_size(files, lines, self.cfg))
        if not report.passed:
            return report
        self.check_logs = []
        self.evidence = []
        self.quality_flags = []
        for r in changed:
            report.extend(_prefixed(self.run_checks(r), r.ref.path if multi else ""))
            if not report.passed:
                return report
        if self.w.features.on("verify"):
            log_path = self.ws.root / ".factory" / "verify.log"
            log_path.write_text("\n\n".join(self.check_logs))
            self.evidence.insert(0, Artifact("verify.log", log_path, "text/plain"))
            for artifact in self.evidence:
                self.rec.artifact(artifact)
        return report

    def run_harness(self, work: Path, task_file: Path, result_file: Path, runtime: SandboxRuntime) -> HarnessResult:
        stage = self.cfg.models.stages.implement
        harness = self.w.c.harness(stage.harness)
        gw_cfg = self.cfg.gateway
        h_cfg = self.cfg.harnesses[stage.harness]
        with Gateway(self.models, stage.provider, stage.model, self.context("implement"), gw_cfg.bind_host,
                     tuple(gw_cfg.dialects)) as gw:
            task = HarnessTask(self.run_id, "implement", self.key, Workspace(self.ws.id, work), task_file, result_file,
                               ModelEndpoint(gw.base_url, gw.token, stage.model, tuple(gw_cfg.dialects)),
                               float(h_cfg.timeout_minutes * 60), h_cfg.max_turns)
            result = harness.run(task, runtime)
        if gw.budget_exceeded:
            raise BudgetExceeded(gw.budget_exceeded)
        return result

    def apply_suggestions(self, work: Path, task_file: Path, result_file: Path, runtime) -> HarnessResult:
        """Deterministic: apply each suggested replacement, bottom-up per file so line numbers stay valid."""
        applied = 0
        by_file: dict[str, list[dict]] = {}
        for s in self.fix.apply:
            by_file.setdefault(s["path"], []).append(s)
        for path, items in by_file.items():
            target = (work / path).resolve()
            if work.resolve() not in target.parents or not target.is_file():
                continue
            lines = target.read_text().splitlines(keepends=True)
            for s in sorted(items, key=lambda s: s["line_start"], reverse=True):
                start, end = s["line_start"] - 1, s["line_end"]
                if 0 <= start < end <= len(lines):
                    replacement = s["replacement"] if s["replacement"].endswith("\n") else s["replacement"] + "\n"
                    lines[start:end] = [replacement]
                    applied += 1
            target.write_text("".join(lines))
        if not applied:
            return HarnessResult(HarnessStatus.BLOCKED, "none of the suggestions could be applied")
        return HarnessResult(HarnessStatus.DONE, f"Applied {applied} suggestion(s).")

    def run_checks(self, r: RepoWork) -> gates.GateReport:
        report = gates.GateReport()
        commands = r.rules.commands
        timeout = float(r.rules.command_timeout_seconds)
        runtime = SandboxRuntime(self.w.sandbox, self.ws, r.subdir)
        label = f"[{r.ref.path}] " if r.dir else ""

        def run(phase: str, argv: list[str]) -> bool:
            res = runtime.run(Command(tuple(argv), ".", {}, timeout))
            self.check_logs.append(f"{label}$ {' '.join(argv)}\nexit {res.exit_code}\n"
                                   f"{res.stdout[-4000:]}{res.stderr[-4000:]}")
            if res.exit_code != 0:
                report.failures.append(f"{phase} failed: {' '.join(argv)} (exit {res.exit_code})")
            return res.exit_code == 0

        for phase, argvs in (("setup", commands.setup), ("lint", commands.lint), ("test", commands.test)):
            for argv in argvs:
                if not run(phase, argv):
                    return report
        report.extend(self.quality(r, run))
        if not report.passed:
            return report
        if self.w.features.on("verify"):
            verify = commands.verify or commands.test
            if not verify and not readiness.text_field(self.ticket, "verify_steps"):
                report.failures.append("no verify or test command is defined")
            for argv in verify:
                if not run("verify", argv):
                    return report
            if r.rules.evidence:
                prefix = r.subdir + "/"
                found = self.w.sandbox.collect_artifacts(self.ws, [prefix + p for p in r.rules.evidence])
                if not found:
                    report.failures.append("no verification evidence was produced (rules.evidence matched nothing)")
                self.evidence += [Artifact(r.shown(a.name[len(prefix):]), a.path, a.media_type) for a in found]
        return report

    def quality(self, r: RepoWork, run) -> gates.GateReport:
        report = gates.GateReport()
        g = self.cfg.gates
        copy, rules = r.copy, r.rules
        changed = [c.path for c in r.found]
        if rules.coverage_report:
            path = copy / rules.coverage_report
            if not path.is_file():
                report.failures.append(f"coverage report {rules.coverage_report} was not produced by the tests")
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
        m = rules.mutation
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
        self.quality_flags += report.flags
        return report

    # ---- review and publish ----------------------------------------------------
    def review(self, diff: str) -> dict:
        if not self.w.features.on("review"):
            return {}
        self.rec.stage("review")
        standards = "\n\n".join(text for _, text in self.standards())
        raw = self.ask_model(
            "review",
            "You are reviewing a change you did not write. Check it against the standards. Reply with JSON "
            '{"summary": "...", "blast_radius": "...", "merge_danger": "low"|"medium"|"high", '
            '"findings": [{"note": "...", "path": "...", "line_start": n, "line_end": n, "replacement": "..."}], '
            '"architecture_diff": "..."}. Give path, lines, and replacement only for concrete suggested changes.',
            f"Standards:\n{standards}\n\nDiff:\n{diff}")
        self.rec.stage_done("review")
        try:
            doc = json.loads(raw)
            return doc if isinstance(doc, dict) else {"summary": raw}
        except json.JSONDecodeError:
            return {"summary": raw}

    def commit(self, r: RepoWork, branch: str, message: str) -> str:
        account = self.w.agent.service_account
        git = Git(GitOptions(clone_depth=0, submodules_enabled=False, author_name=account,
                             author_email=f"{account}@factory.invalid", allow_protocols="file"))
        git.run(["checkout", "-q", "-B", branch], cwd=r.clone_root)
        paths = workspace.apply(r.found, r.copy, r.clone_root)
        workspace.stage_all(git, r.clone_root, paths)
        git.run(["commit", "-q", "-m", message], cwd=r.clone_root)
        self.commit_sha = git.head(r.clone_root)
        return self.commit_sha

    def publish(self, harness_summary: str, report: gates.GateReport) -> RunReport:
        changed = [r for r in self.repos if r.found]
        multi = len(self.repos) > 1
        diff = "".join(workspace.diff_text(r.copy, prefix=f"{r.dir}/" if r.dir else "") for r in changed)
        review = self.review(diff)
        if review:
            self.say("review_done", Note("Review done", "in progress", done=str(review.get("summary", "")),
                                         next="Publishing."))
        if self.w.features.on("verify"):
            self.say("verification_done", Note("Verified", "in progress",
                                               done=f"{len(self.evidence)} evidence artifact(s).", next="Publishing."))
        branch = self.cfg.output.branch_pattern.format(agent=self.w.name, ticket_key=self.key)
        summary = harness_summary
        self.renew()
        descriptions = {}
        for r in changed:
            tracked = [p for p in workspace.tracked_files(r.copy) if p not in agent_kit.PATHS]
            arch = architecture.diagram([(c.status, c.path) for c in r.found], r.copy, tracked)
            self.commit(r, branch, f"{self.key}: {self.ticket.summary}\n\n{summary}")
            others = [o.ref.path for o in changed if o is not r] if multi else []
            descriptions[r.ref.path] = self.mr_description(r, summary, review, report, arch, others)
            template = gates.check_template(descriptions[r.ref.path])
            if not template.passed:
                return self.fail("; ".join(template.failures))
        for r in changed:
            self.w.writes.push_branch(r.clone_root, branch)
        links: dict[str, str] = {"Branch": branch}
        mr_urls = []
        for r in changed:
            suffix = f" ({r.ref.path})" if multi else ""
            if self.cfg.output.mode == "merge_request":
                key = f"{self.run_id}:mr" if r is changed[0] else f"{self.run_id}:mr:{r.ref.path}"
                mr = self.w.writes.open_merge_request(MergeRequestDraft(
                    r.ref, branch, r.ref.default_branch, f"{self.key}: {self.ticket.summary}",
                    descriptions[r.ref.path], key))
                mr_urls.append(mr.url)
                links["Merge request" + suffix] = mr.url
                suggestions = self.post_suggestions(r, mr.id, review)
                if self.w.features.on("mr.read_comments") and not self.w.dry_run:
                    record = self.key if r is changed[0] else f"{self.key}#{r.ref.path}"
                    self.w.mrs.put(record, {
                        "ticket": self.key, "record": record, "repo": r.ref.path, "id": mr.id, "branch": branch,
                        "cursor": None, "run_id": self.run_id, "run_started": self.rec.started.isoformat(),
                        "last_poll": None, "last_activity": self.w.clock.now().isoformat(), "fix_rounds": 0,
                        "suggestions": suggestions, "applies": 0})
                    self.w.mr_index.add(record)
            else:
                compare = self.w.git.compare_url(r.ref, r.ref.default_branch, branch)
                if compare:
                    links["Compare" + suffix] = compare
        outcome = Outcome.MR_OPENED if self.cfg.output.mode == "merge_request" else Outcome.BRANCH_PUSHED
        self.w.writes.transition(self.key, LogicalStatus.IN_REVIEW)
        self.release()
        done = "\n\n".join(descriptions.values()) if self.cfg.output.mode == "branch_only" else summary
        self.say("finished", Note("Ready for human review", "in review", done=done, links=links,
                                  next="A human reviews and merges. The factory never merges."))
        first_link = mr_urls[0] if mr_urls else next((v for k, v in links.items() if k.startswith("Compare")), branch)
        self.w.notify(NotificationKind.MR_READY, self.ticket, f"{self.key} is ready for review", summary, first_link)
        return RunReport(self.key, outcome, summary, self.run_id, mr_urls[0] if mr_urls else "", branch)

    def post_suggestions(self, r: RepoWork, mr_id: str, review: dict) -> list[dict]:
        posted = []
        for i, f in enumerate(review.get("findings") or []):
            if not isinstance(f, dict) or not all(k in f for k in ("path", "line_start", "line_end", "replacement")):
                continue
            path = str(f["path"])
            if r.dir:
                if not path.startswith(r.dir + "/"):
                    continue
                path = path[len(r.dir) + 1:]
            s = Suggestion(path, int(f["line_start"]), int(f["line_end"]), str(f["replacement"]),
                           str(f.get("note", "")))
            key = f"{self.run_id}:suggestion:{i}" if not r.dir else f"{self.run_id}:suggestion:{r.ref.path}:{i}"
            self.w.writes.mr_suggestion(r.ref, mr_id, s, key)
            posted.append({"path": s.path, "line_start": s.line_start, "line_end": s.line_end,
                           "replacement": s.replacement, "note": s.note})
        return posted

    def publish_fix(self, found, stats, summary: str, copy: Path) -> RunReport:
        f = self.fix
        r = self.repos[0]
        if f.apply:
            info = self.w.mrs.get(f.record or self.key) or {}
            n = int(info.get("applies", 0)) + 1
            branch = f"{f.branch}-apply-{n}"
            self.commit(r, branch, f"{self.key}: apply review suggestions\n\n{summary}")
            self.w.writes.push_branch(r.clone_root, branch)
            mr = self.w.writes.open_merge_request(MergeRequestDraft(
                r.ref, branch, f.branch, f"{self.key}: apply review suggestions",
                self.apply_description(found, stats, summary), f"{f.comment_id}:apply"))
            self.reply(f"Opened {mr.url} with the remaining suggestions applied.", "applied")
            return RunReport(self.key, Outcome.FIX_PUSHED, summary, self.run_id, mr.url, branch)
        self.commit(r, f.branch, f"{self.key}: address review feedback\n\n{summary}")
        sha = self.w.writes.push_branch(r.clone_root, f.branch)
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

    def mr_description(self, r: RepoWork, summary: str, review: dict, report: gates.GateReport, arch: str,
                       others: list[str]) -> str:
        changed = "\n".join(f"- {c.status} {c.path}" for c in r.found)
        evidence = "\n".join(f"- {a.name}" for a in self.evidence) or "- (verify disabled)"
        flags = "\n".join(f"- {f}" for f in report.flags)
        findings = "\n".join(f"- {f.get('note', f)}" if isinstance(f, dict) else f"- {f}"
                             for f in review.get("findings") or [])
        reviewer_arch = review.get("architecture_diff", "")
        related = ("\n\nThis ticket also changes: " + ", ".join(others) + " (same branch name in each repo).") \
            if others else ""
        return "\n\n".join([
            f"## What changed\n{summary or '(no summary)'}\n\n{changed}\n\n"
            f"+{r.stats.added} / -{r.stats.deleted} lines{related}",
            f"## Why\n{self.key}: {self.ticket.summary}",
            f"## Blast radius\n{review.get('blast_radius', 'Not reviewed.')}\n\n"
            f"Merge danger: {review.get('merge_danger', 'unknown')}"
            + (f"\n\nReview findings:\n{findings}" if findings else "") + (f"\n\nGate notes:\n{flags}" if flags else ""),
            f"## How verified\n{evidence}",
            f"## Architecture diff\n```plantuml\n{arch}\n```" + (f"\n\n{reviewer_arch}" if reviewer_arch else ""),
        ])


def _prefixed(report: gates.GateReport, repo: str) -> gates.GateReport:
    if not repo:
        return report
    return gates.GateReport([f"{repo}: {f}" for f in report.failures], [f"{repo}: {f}" for f in report.flags])
