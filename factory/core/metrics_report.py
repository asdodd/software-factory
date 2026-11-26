"""Feedback metrics, computed from the trace store (read credentials) and the
state store. Deterministic: no model.

- cost per ticket: tokens per ticket over the period
- MR acceptance rate: merged / (merged + closed) from recorded MR outcomes
- review rounds: fix rounds per closed-out MR
- human override rate at intake: overrides / (overrides + confirmations)
- time per stage: mean seconds per stage
- tool-call success rate: tool results that were not denied or errors"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from statistics import mean

from factory.ports.types import Role, TraceQuery

FAILED_TOOL = ("denied:", "error:", "exit 1", "exit 2", "exit 127")


def compute(reader, state, since: datetime, until: datetime, agent: str | None = None) -> dict:
    tokens_by_ticket: dict[str, int] = defaultdict(int)
    stage_times: dict[str, list[float]] = defaultdict(list)
    outcomes: dict[str, int] = defaultdict(int)
    merged = closed = 0
    fix_rounds: list[int] = []
    tool_ok = tool_total = 0
    runs = 0
    for meta in reader.list(TraceQuery(agent=agent, since=since, until=until)):
        runs += 1
        tokens_by_ticket[meta.ticket_key] += meta.usage.total
        for stage, seconds in meta.stage_seconds.items():
            stage_times[stage].append(seconds)
        if meta.outcome:
            outcomes[meta.outcome.value] += 1
        _, events, recorded = reader.get(meta.run_id)
        for o in recorded:
            if o.kind == "mr_merged":
                merged += 1
                fix_rounds.append(int(o.payload.get("fix_rounds", 0)))
            elif o.kind == "mr_closed":
                closed += 1
                fix_rounds.append(int(o.payload.get("fix_rounds", 0)))
        for e in events:
            if e.kind != "model_call":
                continue
            for m in e.payload.get("messages", []):
                if m.get("role") == Role.TOOL.value:
                    tool_total += 1
                    tool_ok += not str(m.get("content", "")).lstrip().lower().startswith(FAILED_TOOL)
    stats = json.loads(state.get("intake-stats", "all") or "{}")
    overrides = sum(s.get("overrides", 0) for s in stats.values())
    confirmations = sum(s.get("confirmations", 0) for s in stats.values())
    return {
        "period": {"since": since.isoformat(), "until": until.isoformat(), "agent": agent or "all"},
        "runs": runs,
        "outcomes": dict(outcomes),
        "tickets": len(tokens_by_ticket),
        "cost_per_ticket_tokens": round(mean(tokens_by_ticket.values()), 1) if tokens_by_ticket else None,
        "mr_acceptance_rate": round(merged / (merged + closed), 3) if merged + closed else None,
        "review_rounds_mean": round(mean(fix_rounds), 2) if fix_rounds else None,
        "intake_override_rate": round(overrides / (overrides + confirmations), 3) if overrides + confirmations else None,
        "intake_overrides_by_source": stats,
        "stage_seconds_mean": {k: round(mean(v), 3) for k, v in sorted(stage_times.items())},
        "tool_call_success_rate": round(tool_ok / tool_total, 3) if tool_total else None,
    }


def prometheus(report: dict, prefix: str = "factory") -> str:
    lines = []

    def gauge(name: str, value, labels: str = "") -> None:
        if value is not None:
            lines.append(f"{prefix}_{name}{labels} {value}")

    gauge("runs", report["runs"])
    gauge("cost_per_ticket_tokens", report["cost_per_ticket_tokens"])
    gauge("mr_acceptance_rate", report["mr_acceptance_rate"])
    gauge("review_rounds_mean", report["review_rounds_mean"])
    gauge("intake_override_rate", report["intake_override_rate"])
    gauge("tool_call_success_rate", report["tool_call_success_rate"])
    for outcome, n in sorted(report["outcomes"].items()):
        gauge("run_outcomes", n, f'{{outcome="{outcome}"}}')
    for stage, seconds in report["stage_seconds_mean"].items():
        gauge("stage_seconds_mean", seconds, f'{{stage="{stage}"}}')
    return "\n".join(lines) + "\n"
