"""Trace grading with a cheaper model, calibrated against human judgments.

grade(): for recent runs without a grade, summarize the trace deterministically
(stages, tool calls, failed and denied calls, gate failures, test commands)
and ask the grading model for {verbosity 1-5, wasted_tool_calls, excess_tests,
notes}. Grades are written as new outcome objects next to the run.

calibrate(): grade runs that humans have labeled and measure agreement (every
score within tolerance). Grades carry calibrated=true only while the latest
calibration for the current grading model meets min_agreement; the
self-improvement job refuses to use an uncalibrated grader."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from factory.core.budget import BudgetedModels
from factory.core.container import Container
from factory.ports.types import CallContext, Message, OutcomeRecord, Role, TraceQuery

PROMPT = (
    "Grade one agent run from its trace summary. Reply with JSON "
    '{"verbosity": 1-5 (1 terse, 5 rambling), "wasted_tool_calls": <count of calls that did not move the task '
    'forward>, "excess_tests": <count of tests beyond what the change needs>, "notes": "<one sentence>"}.'
)
SCORES = ("verbosity", "wasted_tool_calls", "excess_tests")


@dataclass(frozen=True)
class Calibration:
    agreement: float
    runs: int
    model: str
    trusted: bool


def summarize(meta, events) -> str:
    lines = [f"run {meta.run_id} agent {meta.agent} ticket {meta.ticket_key} outcome "
             f"{meta.outcome.value if meta.outcome else 'unknown'}", f"tokens {meta.usage.total}",
             f"stage seconds {json.dumps(meta.stage_seconds, sort_keys=True)}"]
    tool_calls = failed = 0
    for e in events:
        p = e.payload
        if e.kind == "model_call":
            for call in p.get("tool_calls", []):
                tool_calls += 1
                args = json.dumps(call.get("arguments", {}), sort_keys=True)[:200]
                lines.append(f"tool {call.get('name')} {args}")
            for m in p.get("messages", []):
                if m.get("role") == "tool" and str(m.get("content", "")).startswith(("denied:", "error:")):
                    failed += 1
            if p.get("response"):
                lines.append(f"model said ({p.get('stage')}): {str(p['response'])[:300]}")
        elif e.kind in ("gates", "harness_result", "access_denied", "outcome"):
            lines.append(f"{e.kind}: {json.dumps(p, sort_keys=True)[:400]}")
    lines.insert(2, f"tool calls {tool_calls}, failed or denied {failed}")
    return "\n".join(lines)


class Grader:
    def __init__(self, container: Container) -> None:
        self.c = container
        self.cfg = container.loaded.config
        self.clock = container.port("clock")
        self.state = container.port("state_store")
        self.store = container.trace_store()
        self.stage = self.cfg.models.stages.grading
        self.models = BudgetedModels.for_config(container.provider, container.port("accounting"), self.clock, self.cfg)

    @property
    def model_id(self) -> str:
        return f"{self.stage.provider}/{self.stage.model}"

    def calibration(self) -> Calibration | None:
        raw = self.state.get("grader-calibration", self.model_id)
        if not raw:
            return None
        d = json.loads(raw)
        return Calibration(d["agreement"], d["runs"], d["model"], d["agreement"] >= self.cfg.feedback.grading.min_agreement)

    def score(self, run_id: str) -> dict:
        meta, events, _ = self.store.get(run_id)
        text = self.c.redactor.redact(summarize(meta, events)).text
        context = CallContext("grader", f"grade:{run_id}", "grading", "factory", "factory")
        raw = self.models.complete(self.stage.provider, self.stage.model,
                                   (Message(Role.SYSTEM, PROMPT), Message(Role.USER, text)), context).message.content
        try:
            doc = json.loads(raw)
            return {"verbosity": int(doc["verbosity"]), "wasted_tool_calls": int(doc["wasted_tool_calls"]),
                    "excess_tests": int(doc["excess_tests"]), "notes": str(doc.get("notes", ""))[:500]}
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return {}

    def calibrate(self, labels: Iterable[dict]) -> Calibration:
        tol = self.cfg.feedback.grading.tolerance
        agree = total = 0
        for label in labels:
            got = self.score(str(label["run_id"]))
            total += 1
            agree += bool(got) and all(abs(got[k] - int(label[k])) <= tol for k in SCORES)
        agreement = agree / total if total else 0.0
        self.state.put("grader-calibration", self.model_id, json.dumps(
            {"agreement": agreement, "runs": total, "model": self.model_id, "at": self.clock.now().isoformat()}))
        return Calibration(agreement, total, self.model_id, agreement >= self.cfg.feedback.grading.min_agreement)

    def grade(self) -> list[dict]:
        g = self.cfg.feedback.grading
        now = self.clock.now()
        calibration = self.calibration()
        results = []
        for meta in self.store.list(TraceQuery(since=now - timedelta(days=g.lookback_days), until=now)):
            if len(results) >= g.max_runs_per_job:
                break
            _, _, outcomes = self.store.get(meta.run_id)
            if any(o.kind == "grade" for o in outcomes):
                continue
            scores = self.score(meta.run_id)
            if not scores:
                continue
            payload = {**scores, "grader": self.model_id, "calibrated": bool(calibration and calibration.trusted)}
            self.store.append_outcome(OutcomeRecord(meta.run_id, now, "grade", payload, meta.agent, meta.ticket_key,
                                                    meta.started))
            results.append({"run_id": meta.run_id, **payload})
        self.store.flush()
        return results

