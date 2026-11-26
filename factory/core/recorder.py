"""Collects a run's trace, redacts it, and hands it to the write-only trace
writer. Upload problems are counted, never raised."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from factory import __version__
from factory.ports.types import Artifact, ModelRequest, ModelResponse, Outcome, RunMeta, TraceEvent, Usage
from factory.redaction import Redactor

log = logging.getLogger("factory.trace")


class Recorder:
    def __init__(self, writer, redactor: Redactor, clock, metrics, capture_level: str, *, run_id: str, agent: str,
                 ticket_key: str, config_hash: str) -> None:
        self.writer = writer
        self.redactor = redactor
        self.clock = clock
        self.metrics = metrics
        self.full = capture_level == "full"
        self.run_id = run_id
        self.agent = agent
        self.ticket_key = ticket_key
        self.config_hash = config_hash
        self.repo = ""
        self.models: dict[str, str] = {}
        self.events: list[TraceEvent] = []
        self.artifacts: list[Artifact] = []
        self.stage_seconds: dict[str, float] = {}
        self.usage = Usage(0, 0)
        self.requests = 0
        self.redactions = 0
        self.started = clock.now()
        self._stage_start: dict[str, float] = {}

    def event(self, kind: str, payload: Mapping[str, object], always: bool = False) -> None:
        if not (self.full or always):
            return
        clean, n = self.redactor.redact_obj(dict(payload))
        self.redactions += n
        self.events.append(TraceEvent(self.clock.now(), kind, clean))

    def stage(self, name: str) -> None:
        self._stage_start[name] = time.monotonic()
        self.event("stage", {"stage": name}, always=True)

    def stage_done(self, name: str) -> None:
        if name in self._stage_start:
            self.stage_seconds[name] = round(time.monotonic() - self._stage_start.pop(name), 3)

    def model_call(self, stage: str, provider: str, model: str, request: ModelRequest, response: ModelResponse) -> None:
        self.models[stage] = f"{provider}/{model}"
        self.requests += 1
        self.usage = Usage(self.usage.input_tokens + response.usage.input_tokens,
                           self.usage.output_tokens + response.usage.output_tokens)
        self.event("model_call", {
            "stage": stage, "provider": provider, "model": model,
            "messages": [{"role": m.role.value, "content": m.content,
                          "tool_calls": [{"name": c.name, "arguments": dict(c.arguments)} for c in m.tool_calls]}
                         for m in request.messages],
            "response": response.message.content,
            "tool_calls": [{"name": c.name, "arguments": dict(c.arguments)} for c in response.message.tool_calls],
            "usage": {"input": response.usage.input_tokens, "output": response.usage.output_tokens},
        })

    def artifact(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)

    def finish(self, outcome: Outcome) -> None:
        self.event("redactions", {"count": self.redactions}, always=True)
        meta = RunMeta(1, self.run_id, self.agent, self.ticket_key, self.repo, dict(self.models), __version__,
                       self.config_hash, self.started, self.clock.now(), dict(self.stage_seconds), self.usage, outcome,
                       self.requests)
        try:
            self.writer.put_run(meta, list(self.events), list(self.artifacts))
            self.writer.flush()
        except Exception:  # noqa: BLE001 - trace storage must never fail a ticket
            log.exception("trace upload failed for run %s; it will be retried", self.run_id)
            self.metrics.incr("trace_upload_failures")
        if self.redactions:
            log.info("run %s: %d redactions before upload", self.run_id, self.redactions)
