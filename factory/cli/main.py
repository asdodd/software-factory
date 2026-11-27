"""factory: command-line entry points.

  factory config validate | config show
  factory features [--agent NAME]
  factory agents
  factory run (--agent NAME | --all-agents) [--loop]
  factory scenario run FILE...
  factory workspace init [DIR] | add URL | sync [NAME...] | list | ticket KEY

Common options: --profile, --config-dir, --enable, --disable, --set k=v, --dry-run, --output-mode."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from pathlib import Path

from factory.config.checks import overlapping_agents
from factory.config.loader import LoadedConfig, Overrides, load
from factory.config.secrets import SECRET_KEYS
from factory.config.validate import ConfigError
from factory.redaction import Redactor


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redactor.redact(record.getMessage()).text
        record.args = ()
        return True


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", help="environment profile (default: sim)")
    p.add_argument("--config-dir", default=".", help="directory containing config/ (default: .)")
    p.add_argument("--enable", action="append", default=[], metavar="FEATURE")
    p.add_argument("--disable", action="append", default=[], metavar="FEATURE")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", dest="sets")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--output-mode", choices=["merge_request", "branch_only"])


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="factory", description="Software factory")
    sub = root.add_subparsers(dest="command", required=True)
    config = sub.add_parser("config", help="validate or show config")
    csub = config.add_subparsers(dest="action", required=True)
    for name in ("validate", "show"):
        p = csub.add_parser(name)
        _common(p)
        if name == "show":
            p.add_argument("--agent")
    p = sub.add_parser("features", help="list features, state, source, dependencies")
    _common(p)
    p.add_argument("--agent")
    p = sub.add_parser("agents", help="list agents, filters, and overlaps")
    _common(p)
    p = sub.add_parser("run", help="run agents")
    _common(p)
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--agent")
    who.add_argument("--all-agents", action="store_true")
    p.add_argument("--loop", action="store_true", help="keep polling instead of running one cycle")
    p.add_argument("--interval", type=float, default=60.0)
    p = sub.add_parser("intake", help="poll enabled intake sources (email, GitLab comments) and reconcile overrides")
    _common(p)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=float, default=60.0)
    p = sub.add_parser("metrics", help="feedback metrics from traces and intake state")
    _common(p)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--agent")
    p.add_argument("--format", choices=["json", "prometheus"], default="json")
    p = sub.add_parser("grade", help="grade recent traces, or calibrate the grader against human labels")
    _common(p)
    p.add_argument("--calibrate", metavar="LABELS.jsonl",
                   help="JSON lines: run_id, verbosity, wasted_tool_calls, excess_tests")
    p = sub.add_parser("improve", help="propose factory improvements as a merge request (never applies them)")
    _common(p)
    scen = sub.add_parser("scenario", help="run declarative simulation scenarios")
    ssub = scen.add_subparsers(dest="action", required=True)
    p = ssub.add_parser("run")
    p.add_argument("files", nargs="+")
    wsp = sub.add_parser("workspace", help="a local workspace: the agent kit plus repos you clone, no pipeline")
    wsub = wsp.add_subparsers(dest="action", required=True)
    p = wsub.add_parser("init", help="install the agent kit and a workspace.toml into DIR (default: here)")
    p.add_argument("dir", nargs="?", default=".")
    p = wsub.add_parser("add", help="list a repo in workspace.toml and clone it")
    p.add_argument("url")
    p.add_argument("--name", default="", help="directory under repos/ (default: from the URL)")
    p.add_argument("--branch", default="")
    p.add_argument("--no-clone", action="store_true")
    p.add_argument("--root", help="workspace root (default: found from the current directory)")
    p = wsub.add_parser("sync", help="clone missing repos and fast-forward the rest")
    p.add_argument("names", nargs="*")
    p.add_argument("--root")
    p = wsub.add_parser("list", help="repos in the workspace and whether they are cloned")
    p.add_argument("--root")
    p = wsub.add_parser("ticket", help="write a ticket's text into tickets/KEY and clone its repos")
    p.add_argument("key")
    p.add_argument("--no-clone", action="store_true")
    p.add_argument("--root")
    _common(p)
    return root


def _load(args) -> LoadedConfig:
    return load(Path(args.config_dir), args.profile, None,
                Overrides(tuple(args.sets), tuple(args.enable), tuple(args.disable), args.dry_run, args.output_mode))


def _print_diagnostics(loaded: LoadedConfig) -> None:
    for d in loaded.diagnostics:
        tag = {"alert": "ERROR (non-fatal)", "warning": "warning"}.get(d.level, d.level)
        print(f"{tag}: {d.message}", file=sys.stderr)


def _show(obj: object, key: str = "") -> object:
    if dataclasses.is_dataclass(obj):
        return {f.name: _show(getattr(obj, f.name), f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: _show(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_show(v, key) for v in obj]
    if key in SECRET_KEYS and isinstance(obj, str) and obj:
        return "***"
    return obj


def cmd_config(args) -> int:
    loaded = _load(args)
    if args.action == "validate":
        _print_diagnostics(loaded)
        agents = ", ".join(loaded.enabled_agents()) or "none"
        print(f"config OK (profile {loaded.profile}, agents: {agents}, hash {loaded.config_hash})")
        return 0
    cfg = loaded.agent_configs[args.agent] if args.agent else loaded.config
    print(json.dumps(_show(cfg), indent=2, sort_keys=True))
    return 0


def cmd_features(args) -> int:
    loaded = _load(args)
    states = loaded.agent_features[args.agent] if args.agent else loaded.features
    rows = [("feature", "state", "source", "requires")]
    for s in states.values():
        req = ", ".join(s.requires) + (f" output.mode={s.requires_output_mode}" if s.requires_output_mode else "")
        rows.append((s.name, "on" if s.enabled else "off", s.source, req.strip() or "-"))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())
    return 0


def cmd_agents(args) -> int:
    loaded = _load(args)
    for name, agent in loaded.agents.items():
        f = agent.filter
        dims = {k: v for k, v in dataclasses.asdict(f).items() if v}
        cfg = loaded.agent_configs[name]
        print(f"{name}  ({'enabled' if agent.enabled else 'disabled'})  repos={agent.repos}  "
              f"mode={cfg.output.mode}  ready_label={cfg.labels.ready}")
        print(f"    filter: {json.dumps(dims, sort_keys=True)}")
    for x, y in overlapping_agents(loaded.agents):
        print(f"overlap: {x} and {y} may match the same tickets (leases prevent double claims)")
    return 0


def cmd_run(args) -> int:
    from factory.core.container import Container
    from factory.core.worker import Worker

    loaded = _load(args)
    _print_diagnostics(loaded)
    if loaded.profile == "sim":
        from factory.testing import netguard
        netguard.install()
    container = Container(loaded)
    logging.getLogger().addFilter(RedactingFilter(container.redactor))
    names = loaded.enabled_agents() if args.all_agents else [args.agent]
    unknown = [n for n in names if n not in loaded.agents]
    if unknown:
        print(f"unknown agent(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    workers = [Worker(container, n) for n in names]
    while True:
        for worker in workers:
            for report in worker.run_once():
                print(f"{worker.name}  {report.ticket_key}  {report.outcome.value}  {report.mr_url or report.branch}")
        if not args.loop:
            return 0
        time.sleep(args.interval)


def cmd_intake(args) -> int:
    from factory.core.container import Container
    from factory.core.intake_sources import IntakeRunner

    loaded = _load(args)
    _print_diagnostics(loaded)
    if loaded.profile == "sim":
        from factory.testing import netguard
        netguard.install()
    container = Container(loaded)
    logging.getLogger().addFilter(RedactingFilter(container.redactor))
    runner = IntakeRunner(container)
    while True:
        for result in runner.run_once():
            state = "duplicate of " + result.duplicate_of if result.duplicate_of else (
                "ready" if result.ready else f"verdict {result.verdict}")
            print(f"intake  {result.ticket_key}  {state}  {result.routed_to}".rstrip())
        if not args.loop:
            return 0
        time.sleep(args.interval)


def _container(args):
    from factory.core.container import Container

    loaded = _load(args)
    _print_diagnostics(loaded)
    if loaded.profile == "sim":
        from factory.testing import netguard
        netguard.install()
    container = Container(loaded)
    logging.getLogger().addFilter(RedactingFilter(container.redactor))
    return loaded, container


def _require(loaded, feature: str) -> bool:
    if not loaded.features.on(feature):
        print(f"feature {feature} is off; enable it in config or with --enable {feature}", file=sys.stderr)
        return False
    return True


def cmd_metrics(args) -> int:
    from datetime import timedelta

    from factory.core import metrics_report

    loaded, container = _container(args)
    if not _require(loaded, "feedback.metrics") or not _require(loaded, "feedback.trace_store"):
        return 2
    now = container.port("clock").now()
    report = metrics_report.compute(container.trace_store(), container.port("state_store"),
                                    now - timedelta(days=args.days), now, args.agent)
    print(metrics_report.prometheus(report) if args.format == "prometheus" else json.dumps(report, indent=2))
    return 0


def cmd_grade(args) -> int:
    from factory.core.grading import Grader

    loaded, container = _container(args)
    if not _require(loaded, "feedback.grading"):
        return 2
    grader = Grader(container)
    if args.calibrate:
        labels = [json.loads(line) for line in Path(args.calibrate).read_text().splitlines() if line.strip()]
        cal = grader.calibrate(labels)
        verdict = "trusted" if cal.trusted else "NOT trusted"
        print(f"grader {cal.model}: agreement {cal.agreement:.0%} over {cal.runs} labeled run(s); {verdict}")
        return 0 if cal.trusted else 1
    for g in grader.grade():
        print(json.dumps(g, sort_keys=True))
    return 0


def cmd_improve(args) -> int:
    from factory.core.improve import SelfImprovement

    loaded, container = _container(args)
    if not _require(loaded, "feedback.self_improvement"):
        return 2
    proposal = SelfImprovement(container).run()
    print(f"{'opened ' + proposal.mr_url if proposal.opened else 'no proposal'}: {proposal.reason}")
    return 0


def cmd_scenario(args) -> int:
    from factory.testing import netguard
    from factory.testing.scenario import run_file

    netguard.install()
    failed = 0
    for path in args.files:
        result = run_file(path)
        print(("PASS " if result.passed else "FAIL ") + f"{path}: {result.name}")
        for failure in result.failures:
            print(f"    {failure}")
        failed += not result.passed
    return 1 if failed else 0


def cmd_workspace(args) -> int:
    from factory import local

    try:
        if args.action == "init":
            lines = local.init(Path(args.dir))
            lines.append(f"workspace ready at {Path(args.dir).resolve()}: add repos with `factory workspace add URL`, "
                         "then start your agent there")
        else:
            root = Path(args.root).resolve() if args.root else local.find_root(Path.cwd())
            if args.action == "add":
                entry = local.add(root, args.url, args.name, args.branch)
                lines = [f"listed {entry.name}"] + ([] if args.no_clone else local.sync(root, [entry.name]))
            elif args.action == "sync":
                lines = local.sync(root, args.names)
            elif args.action == "list":
                lines = local.status(root)
            else:
                from factory.core.container import Container

                tickets = Container(_load(args)).port("ticket_system")
                lines = local.ticket(root, tickets, args.key, clone=not args.no_clone)
    except local.WorkspaceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = parser().parse_args(argv)
    handlers = {"config": cmd_config, "features": cmd_features, "agents": cmd_agents, "run": cmd_run,
                "scenario": cmd_scenario, "intake": cmd_intake,
                "metrics": cmd_metrics, "grade": cmd_grade, "improve": cmd_improve, "workspace": cmd_workspace}
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
