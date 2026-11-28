"""Declarative end-to-end scenarios against the fakes.

A scenario file (TOML) gives a starting state (repos, tickets, scripted model
and harness behavior, race hooks), a list of steps, and the expected end state.
`factory scenario run <file>` and the pytest suite both use this runner."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from factory.config.loader import Layer, Overrides, load
from factory.config.validate import ConfigError
from factory.core.container import Container
from factory.core.intake import IntakeEvent, IntakeService
from factory.core.worker import RunReport, Worker
from factory.core.writes import Writes

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ScenarioResult:
    name: str
    failures: list[str] = field(default_factory=list)
    reports: list[RunReport] = field(default_factory=list)
    container: Container | None = None
    workdir: Path | None = None
    grades: list = field(default_factory=list)
    calibration: object | None = None
    proposal: object | None = None
    metrics: dict | None = None
    fake_s3: object | None = None

    @property
    def passed(self) -> bool:
        return not self.failures


def _merge(base: dict, top: dict) -> dict:
    out = dict(base)
    for key, value in top.items():
        if isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = [*out[key], *value]
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_file(path: Path) -> dict:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    merged: dict = {}
    for inc in data.pop("include", []):
        merged = _merge(merged, load_file(path.parent / inc))
    return _merge(merged, data)


def run_file(path: Path | str, root: Path = REPO_ROOT, keep: bool = False) -> ScenarioResult:
    path = Path(path)
    data = load_file(path)
    workdir = Path(tempfile.mkdtemp(prefix="factory-scenario-"))
    process_env = _substitute(data.get("process_env", {}), {
        "workdir": str(workdir), "scenario_dir": str(path.parent.resolve()), "PATH": os.environ.get("PATH", "")})
    saved = {k: os.environ.get(k) for k in process_env}
    os.environ.update({k: str(v) for k, v in process_env.items()})
    try:
        return run(data, root, workdir, path.parent)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def _substitute(obj: object, values: dict[str, str]) -> object:
    if isinstance(obj, str):
        for name, value in values.items():
            obj = obj.replace("{" + name + "}", value)
        return obj
    if isinstance(obj, dict):
        return {k: _substitute(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, values) for v in obj]
    return obj


def run(data: dict, root: Path, workdir: Path, scenario_dir: Path) -> ScenarioResult:
    data = _substitute(data, {"workdir": str(workdir), "scenario_dir": str(scenario_dir.resolve())})
    result = ScenarioResult(data.get("name", "unnamed"), workdir=workdir)
    expect = data.get("expect", {})
    agents_dir = None
    if data.get("agent_files"):
        agents_dir = workdir / "agents"
        agents_dir.mkdir()
        for rel in data["agent_files"]:
            shutil.copy(scenario_dir / rel, agents_dir / Path(rel).name)
    ov = data.get("overrides", {})
    overrides = Overrides(tuple(ov.get("set", ())), tuple(ov.get("enable", ())), tuple(ov.get("disable", ())),
                          bool(ov.get("dry_run", False)), ov.get("output_mode"))
    env = {str(k): str(v) for k, v in data.get("env", {}).items()}
    settings = dict(data.get("settings", {}))
    repo_rules = {repo["path"]: repo["rules"] for repo in data.get("repos", []) if "rules" in repo}
    if repo_rules:  # a fixture repo's factory-side settings: [repos."path"] in config
        settings["repos"] = {**repo_rules, **settings.get("repos", {})}
    try:
        loaded = load(root, data.get("profile", "sim"), env, overrides,
                      [Layer("scenario", settings)], agents_dir)
    except ConfigError as exc:
        wanted = expect.get("startup_error")
        if not wanted:
            result.failures.append(f"unexpected startup error: {exc}")
        elif not any(wanted in p for p in exc.problems):
            result.failures.append(f"startup error did not mention {wanted!r}: {exc.problems}")
        return result
    if expect.get("startup_error"):
        result.failures.append(f"expected startup error {expect['startup_error']!r}, but config loaded")
        return result
    for text in expect.get("warnings_contain", []):
        if not any(text in d.message for d in loaded.warnings()):
            result.failures.append(f"no warning mentions {text!r}: {[d.message for d in loaded.warnings()]}")

    if data.get("fake_s3"):
        from factory.adapters.fake.s3 import FakeS3
        from factory.adapters.trace import s3 as s3_module
        result.fake_s3 = FakeS3(loaded.config.trace_store.s3.bucket)
        s3_module.CLIENT_FACTORY = result.fake_s3.factory
    c = Container(loaded, env, base_dir=workdir)
    result.container = c
    tickets = c.port("ticket_system")
    git = c.port("git_host")
    for repo in data.get("repos", []):
        git.create_repo(repo["path"], repo.get("files", {}), repo.get("branch", "main"), repo.get("submodules"),
                        repo.get("symlinks"))
    for commit in data.get("commits", []):
        git.commit(commit["path"], commit.get("files", {}), commit.get("branch", "main"))
    for gone in data.get("remove_repos", []):
        shutil.rmtree(git.repos_root / f"{gone}.git")
    if data.get("trace_fail_puts"):
        c.trace_store().backend.fail_puts = int(data["trace_fail_puts"])
    for t in data.get("tickets", []):
        tickets.seed(**t)
    for att in data.get("attachments", []):
        tickets.attach(att["ticket"], att["name"], att["content"].encode())
    for script in data.get("model", []):
        script = dict(script)
        provider = c.provider(script.pop("provider", "scripted"))
        provider.queue(**script)
    for script in data.get("harness", []):
        c.harness(script.get("name", "scripted")).queue(script["ticket"], script.get("actions", []))
    for hook in data.get("hooks", []):
        tickets.after(hook["after"], _hook(hook))

    workers: dict[str, Worker] = {}

    def worker(agent: str, wid: str | None) -> Worker:
        name = f"{agent}:{wid}" if wid else agent
        if name not in workers:
            workers[name] = Worker(c, agent, f"{agent}:{wid}" if wid else None)
        return workers[name]

    for step in data.get("steps", []):
        if "run" in step:
            result.reports += worker(step["run"], step.get("worker")).run_once()
        elif step.get("run_all"):
            for agent in loaded.enabled_agents():
                result.reports += worker(agent, None).run_once()
        elif "advance_minutes" in step:
            c.port("clock").advance(minutes=step["advance_minutes"])
        elif "intake" in step:
            ev = dict(step["intake"])
            ev["components"] = tuple(ev.get("components", ()))
            writes = Writes(tickets, git, c.port("state_store"), c.port("clock"), c.redactor, loaded.config,
                            "intake", loaded.features.on("dry_run"))
            IntakeService(loaded, tickets, c.port("state_store"), c.port("clock"), writes).submit(IntakeEvent(**ev))
        elif "human_comment" in step:
            hc = step["human_comment"]
            tickets.add_human_comment(hc["ticket"], hc["author"], hc["body"], hc.get("groups", ()))
        elif "email" in step:
            from factory.ports.types import MailAttachment, MailMessage
            e = dict(step["email"])
            n = len(c.port("mailbox").messages) + 1
            atts = tuple(MailAttachment(a["name"], a.get("type", "text/plain"), a["content"].encode())
                         for a in e.get("attachments", []))
            c.port("mailbox").deliver(MailMessage(
                e.get("message_id", f"<m{n}@sim.invalid>"), e.get("thread", f"thread-{n}"), e.get("in_reply_to"),
                e["from"], tuple(e.get("to", ["factory@sim.invalid"])), tuple(e.get("cc", [])),
                e.get("subject", "Request"), e.get("body", ""), dict(e.get("headers", {})), c.port("clock").now(), atts))
        elif "gitlab_comment" in step:
            from factory.ports.types import PrincipalKind
            gc = dict(step["gitlab_comment"])
            git.add_comment_event(gc["repo"], gc["author"], gc["body"], gc.get("target_kind", "merge_request"),
                                  str(gc.get("target_id", "1")), gc.get("event_id"),
                                  PrincipalKind(gc.get("kind", "user")))
        elif "monitor_finding" in step:
            from factory.ports.types import Finding
            mf = dict(step["monitor_finding"])
            c.port("monitor").findings.append(Finding(
                mf["id"], mf.get("source", "regression"), mf.get("repo", ""), mf.get("location", ""), mf["summary"],
                mf.get("details", ""), mf.get("severity", "high")))
        elif "calibrate" in step:
            from factory.core.grading import Grader
            result.calibration = Grader(c).calibrate(step["calibrate"])
        elif step.get("grade"):
            from factory.core.grading import Grader
            result.grades += Grader(c).grade()
        elif step.get("improve"):
            from factory.core.improve import SelfImprovement
            result.proposal = SelfImprovement(c).run()
        elif step.get("metrics"):
            from datetime import timedelta

            from factory.core import metrics_report
            now = c.port("clock").now()
            result.metrics = metrics_report.compute(c.trace_store(), c.port("state_store"), now - timedelta(days=30),
                                                    now)
        elif step.get("intake_poll"):
            from factory.core.intake_sources import IntakeRunner
            if "intake" not in workers:
                workers["intake"] = IntakeRunner(c)
            workers["intake"].run_once()
        elif "advance_hours" in step:
            c.port("clock").advance(hours=step["advance_hours"])
        elif "mr_comment" in step:
            mc = step["mr_comment"]
            git.add_mr_comment(str(mc.get("mr", "1")), mc["author"], mc["body"], mc.get("thread"))
        elif "set_mr_state" in step:
            from factory.ports.types import MergeRequestState
            ms = step["set_mr_state"]
            git.set_mr_state(str(ms.get("mr", "1")), MergeRequestState(ms["state"]))
        elif "set_labels" in step:
            sl = step["set_labels"]
            tickets.set_labels(sl["ticket"], sl.get("add", ()), sl.get("remove", ()))
        else:
            result.failures.append(f"unknown step {step}")
    try:
        check(result, expect, c, workers)
    finally:
        if data.get("fake_s3"):
            from factory.adapters.trace import s3 as s3_module
            s3_module.CLIENT_FACTORY = None
    return result


def _hook(hook: dict):
    def apply(ts, op, key):
        target = hook.get("ticket", key)
        if hook.get("action") == "set_field":
            ticket = ts.tickets[target]
            ts.force_update(target, fields={**ticket.fields, hook["field"]: hook["value"]})
        elif hook.get("action") == "set_labels":
            ticket = ts.tickets[target]
            labels = (set(ticket.labels) | set(hook.get("add", ()))) - set(hook.get("remove", ()))
            ts.force_update(target, labels=frozenset(labels))
    return apply


def _trace_blobs(c: Container) -> list[str]:
    store = c.trace_store() if c._trace is not None or c.loaded.features.on("feedback.trace_store") else None
    if store is None:
        return []
    texts = []
    backend = store.backend
    for key in backend.list(""):
        data = backend.get(key)
        if key.endswith(".gz"):
            data = gzip.decompress(data)
        texts.append(data.decode(errors="replace"))
    return texts


def check(result: ScenarioResult, expect: dict, c: Container, workers: dict[str, Worker]) -> None:
    fail = result.failures.append
    fb = expect.get("feedback", {})
    if "grades" in fb and len(result.grades) != fb["grades"]:
        fail(f"{len(result.grades)} grades != {fb['grades']}")
    if "grades_calibrated" in fb and any(g["calibrated"] != fb["grades_calibrated"] for g in result.grades):
        fail(f"grades calibrated flags {[g['calibrated'] for g in result.grades]} != {fb['grades_calibrated']}")
    if "calibration_trusted" in fb and (result.calibration is None or result.calibration.trusted != fb["calibration_trusted"]):
        fail(f"calibration {result.calibration} trusted != {fb['calibration_trusted']}")
    if "proposal_opened" in fb and (result.proposal is None or result.proposal.opened != fb["proposal_opened"]):
        fail(f"proposal {result.proposal} opened != {fb['proposal_opened']}")
    if "proposal_reason_contains" in fb and fb["proposal_reason_contains"] not in getattr(result.proposal, "reason", ""):
        fail(f"proposal reason {getattr(result.proposal, 'reason', None)!r} lacks {fb['proposal_reason_contains']!r}")
    if "proposal_files" in fb and list(getattr(result.proposal, "files", ())) != fb["proposal_files"]:
        fail(f"proposal files {getattr(result.proposal, 'files', None)} != {fb['proposal_files']}")
    for key, want in fb.get("metrics", {}).items():
        got = (result.metrics or {}).get(key)
        if got != want:
            fail(f"metrics report {key} = {got!r}, expected {want!r}")
    s3 = expect.get("s3", {})
    if s3:
        bucket = result.fake_s3.bucket if result.fake_s3 else None
        objects = bucket.objects if bucket else {}
        if "objects_at_least" in s3 and len(objects) < s3["objects_at_least"]:
            fail(f"{len(objects)} S3 objects < {s3['objects_at_least']}")
        if s3.get("all_encrypted") and not all(e["ServerSideEncryption"] for e in bucket.encryption.values()):
            fail("an S3 object was stored without server-side encryption")
        for text in s3.get("excludes", []):
            import gzip as _gz
            for key, data in objects.items():
                blob = _gz.decompress(data) if key.endswith(".gz") else data
                if text.encode() in blob:
                    fail(f"S3 object {key} contains {text!r}")
    tickets = c.port("ticket_system")
    git = c.port("git_host")
    if "outcomes" in expect:
        got = [r.outcome.value for r in result.reports if r.outcome.value != "claim_lost"]
        if got != expect["outcomes"]:
            fail(f"outcomes {got} != expected {expect['outcomes']}; details: {[r.detail for r in result.reports]}")
    for t in expect.get("tickets", []):
        key = t["key"]
        ticket = tickets.tickets.get(key)
        if ticket is None:
            fail(f"{key}: ticket does not exist")
            continue
        bot = [cm.body for cm in tickets.comments_by_bot(key)]
        if "status" in t and ticket.status != t["status"]:
            fail(f"{key}: status {ticket.status!r} != {t['status']!r}")
        for label in t.get("labels_include", []):
            if label not in ticket.labels:
                fail(f"{key}: missing label {label!r} (has {sorted(ticket.labels)})")
        for label in t.get("labels_exclude", []):
            if label in ticket.labels:
                fail(f"{key}: unexpected label {label!r}")
        for text in t.get("comments_contain", []):
            if not any(text in body for body in bot):
                fail(f"{key}: no bot comment contains {text!r}; comments: {bot}")
        for text in t.get("comments_exclude", []):
            if any(text in body for body in bot):
                fail(f"{key}: a bot comment contains forbidden text {text!r}")
        if "bot_comments" in t and len(bot) != t["bot_comments"]:
            fail(f"{key}: {len(bot)} bot comments != {t['bot_comments']}: {bot}")
        if "assignee" in t:
            got = ticket.assignee.id if ticket.assignee else ""
            if got != t["assignee"]:
                fail(f"{key}: assignee {got!r} != {t['assignee']!r}")
        if t.get("lease_released") and ticket.fields.get("lease_owner"):
            fail(f"{key}: lease still held by {ticket.fields.get('lease_owner')!r}")
        if "lease_owner" in t and ticket.fields.get("lease_owner") != t["lease_owner"]:
            fail(f"{key}: lease owner {ticket.fields.get('lease_owner')!r} != {t['lease_owner']!r}")
        if "linked_to" in t and not any(s == key and d == t["linked_to"] for s, d, _ in tickets.links):
            fail(f"{key}: not linked to {t['linked_to']}")
    if "ticket_count" in expect and len(tickets.tickets) != expect["ticket_count"]:
        fail(f"{len(tickets.tickets)} tickets != {expect['ticket_count']}")
    g = expect.get("git", {})
    if "mrs" in g and len(git.mrs) != g["mrs"]:
        fail(f"{len(git.mrs)} merge requests != {g['mrs']}")
    pushed = [b for _, b, _ in git.pushes]
    for branch in g.get("pushed_branches", []):
        if branch not in pushed:
            fail(f"branch {branch!r} was not pushed (pushed: {pushed})")
    if g.get("no_pushes") and pushed:
        fail(f"expected no pushes, got {pushed}")
    for text in g.get("mr_description_contains", []):
        if not any(text in d for d in git.mr_descriptions.values()):
            fail(f"no MR description contains {text!r}")
    for text in g.get("mr_description_exclude", []):
        if any(text in d for d in git.mr_descriptions.values()):
            fail(f"an MR description contains forbidden text {text!r}")
    for bf in g.get("branch_files", []):
        files = git.branch_files(bf["path"], bf["branch"])
        for name, needle in bf.get("contains", {}).items():
            if needle not in files.get(name, ""):
                fail(f"{bf['branch']}:{name} does not contain {needle!r}")
        for name in bf.get("absent", []):
            if name in files:
                fail(f"{bf['branch']}:{name} should not exist")
        for name in bf.get("unchanged", {}):
            if files.get(name) != bf["unchanged"][name]:
                fail(f"{bf['branch']}:{name} was modified")
    for port in expect.get("never_instantiated", []):
        if any(label == port or label.startswith(port + ":") for label in c.instantiated):
            fail(f"{port} was instantiated although its feature is off")
    for port in expect.get("instantiated", []):
        if not any(label == port or label.startswith(port + ":") for label in c.instantiated):
            fail(f"{port} was not instantiated")
    for name, want in expect.get("metrics", {}).items():
        got = c.metrics.count(name)
        if got != want:
            fail(f"metric {name} = {got}, expected {want}")
    tr = expect.get("trace", {})
    if tr:
        blobs = _trace_blobs(c)
        metas = [b for b in blobs if '"config_hash"' in b and '"run_id"' in b and '"schema_version"' in b]
        if "runs" in tr and len(metas) != tr["runs"]:
            fail(f"{len(metas)} runs in the trace store != {tr['runs']}")
        for text in tr.get("excludes", []):
            if any(text in b for b in blobs):
                fail(f"trace store contains forbidden text {text!r}")
        for text in tr.get("includes", []):
            if not any(text in b for b in blobs):
                fail(f"trace store lacks {text!r}")
    ho = expect.get("harness_observed")
    if ho:
        observations = [o for name in list(c._harnesses) for o in getattr(c._harnesses[name], "observations", [])]
        if not observations:
            fail("the harness made no observations")
        for obs in observations:
            for f in ho.get("files_exclude", []):
                if any(f in path for path in obs["files"]):
                    fail(f"harness could see {f!r}: {obs['files']}")
            for text in ho.get("history_excludes", []):
                if text in obs["history"]:
                    fail(f"harness could read {text!r} from git history")
            for f in ho.get("files_include", []):
                if f not in obs["files"]:
                    fail(f"harness could not see {f!r}")
            for text in ho.get("instructions_contain", []):
                if text not in obs["instructions"]:
                    fail(f"instructions lack {text!r}")
            for var in ho.get("env_excludes", []):
                if var in obs["env_keys"]:
                    fail(f"harness environment contains {var}")
        hooks = [h for name in list(c._harnesses) for h in getattr(c._harnesses[name], "hook_results", [])]
        for want, blocked in ((ho.get("hook_blocked", []), True), (ho.get("hook_allowed", []), False)):
            for target in want:
                match = [h for h in hooks if target in json.dumps(h["input"])]
                if not match:
                    fail(f"the workspace hook was not run for {target!r}")
                elif any(h["blocked"] != blocked for h in match):
                    fail(f"the workspace hook {'allowed' if blocked else 'blocked'} {target!r}: {match}")
                elif blocked and any("TOPSECRET" in h["message"] for h in match):
                    fail(f"the hook message for {target!r} leaked file content")
    bot_id = git.identity.id
    replies = [cm.body for cms in git.mr_comments.values() for cm in cms if cm.author.id == bot_id]
    for text in g.get("mr_replies_contain", []):
        if not any(text in r for r in replies):
            fail(f"no MR reply contains {text!r}; replies: {replies}")
    if "mr_replies" in g and len(replies) != g["mr_replies"]:
        fail(f"{len(replies)} MR replies != {g['mr_replies']}: {replies}")
    if "suggestions" in g:
        n = sum(len(v) for v in git.suggestions.values())
        if n != g["suggestions"]:
            fail(f"{n} suggestions posted != {g['suggestions']}")
    for branch, count in g.get("push_counts", {}).items():
        n = sum(1 for _, b, _ in git.pushes if b == branch)
        if n != count:
            fail(f"{n} pushes to {branch} != {count} (pushes: {pushed})")
    for mr_id, target in g.get("mr_targets", {}).items():
        mr = git.mrs.get(mr_id)
        if mr is None or mr.target_branch != target:
            fail(f"MR {mr_id} target {getattr(mr, 'target_branch', None)!r} != {target!r}")
    state = c.port("state_store")
    for t in expect.get("tickets", []):
        if "paused" in t:
            paused = any(state.get(f"paused:{a}", t["key"]) for a in c.loaded.agents)
            if paused != t["paused"]:
                fail(f"{t['key']}: paused is {paused}, expected {t['paused']}")
    if "notified" in expect:
        sent = c.port("notifier").sent if "notifier" in c._ports else []
        got = [[n.kind.value, sorted(n.recipients)] for n in sent]
        for want in expect["notified"]:
            if [want[0], sorted(want[1])] not in got:
                fail(f"no notification {want}; sent: {got}")
    if "trace_pending_at_least" in expect:
        pending = c.trace_store().pending
        if pending < expect["trace_pending_at_least"]:
            fail(f"{pending} trace objects pending retry < {expect['trace_pending_at_least']}")
    if "notifications" in expect:
        sent = c.port("notifier").sent if "notifier" in c._ports else []
        if len(sent) != expect["notifications"]:
            fail(f"{len(sent)} notifications != {expect['notifications']}")
    em = expect.get("email", {})
    if em:
        sent = list(c.port("mailbox").sent.values()) if "mailbox" in c._ports else []
        if "replies" in em and len(sent) != em["replies"]:
            fail(f"{len(sent)} email replies != {em['replies']}: {[r.body for r in sent]}")
        for text in em.get("reply_contains", []):
            if not any(text in r.body for r in sent):
                fail(f"no email reply contains {text!r}: {[r.body for r in sent]}")
        for text in em.get("reply_excludes", []):
            if any(text in r.body for r in sent):
                fail(f"an email reply contains {text!r}")
        if "reply_to" in em and not any(list(r.to) == em["reply_to"] for r in sent):
            fail(f"no reply addressed to {em['reply_to']}: {[list(r.to) for r in sent]}")
        if "reply_cc" in em and not any(sorted(r.cc) == sorted(em["reply_cc"]) for r in sent):
            fail(f"no reply with cc {em['reply_cc']}: {[list(r.cc) for r in sent]}")
    for text in g.get("host_comments_contain", []):
        if not any(text in body for *_, body in git.target_comments):
            fail(f"no host comment contains {text!r}: {git.target_comments}")
    if "host_comments" in g and len(git.target_comments) != g["host_comments"]:
        fail(f"{len(git.target_comments)} host comments != {g['host_comments']}")
    for t in expect.get("tickets", []):
        ticket = tickets.tickets.get(t["key"])
        if ticket is not None and "reporter" in t:
            got = ticket.reporter.id if ticket.reporter else ""
            if got != t["reporter"]:
                fail(f"{t['key']}: reporter {got!r} != {t['reporter']!r}")
        if ticket is not None and "field_contains" in t:
            for name, needle in t["field_contains"].items():
                if needle not in str(ticket.fields.get(name, "")):
                    fail(f"{t['key']}: field {name} lacks {needle!r}: {ticket.fields.get(name)!r}")
        if ticket is not None and "summary" in t and ticket.summary != t["summary"]:
            fail(f"{t['key']}: summary {ticket.summary!r} != {t['summary']!r}")
    if "overrides" in expect:
        state = c.port("state_store")
        for key, outcome in expect["overrides"].items():
            raw = state.get("intake-overrides", key)
            got = json.loads(raw)["outcome"] if raw else None
            if got != outcome:
                fail(f"{key}: override outcome {got!r} != {outcome!r}")
    if "intended_writes_at_least" in expect:
        n = sum(len(w.writes.intended) for w in workers.values())
        if n < expect["intended_writes_at_least"]:
            fail(f"{n} intended dry-run writes < {expect['intended_writes_at_least']}")
    for name, spec in expect.get("model_requests", {}).items():
        texts = [m.content for r in c.provider(name).requests for m in r.messages]
        for needle in spec.get("contain", []):
            if not any(needle in t for t in texts):
                fail(f"no request to {name} contains {needle!r}")
        for needle in spec.get("exclude", []):
            if any(needle in t for t in texts):
                fail(f"a request to {name} contains forbidden text {needle!r}")
    for name, count in expect.get("model_calls", {}).items():
        reqs = c.provider(name).requests
        if len(reqs) != count:
            fail(f"{len(reqs)} calls to provider {name} != {count}")
