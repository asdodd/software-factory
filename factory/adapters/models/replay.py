"""Record-and-replay model provider. In record mode it wraps another
configured provider and appends each request/response to a JSONL file; in
replay mode it answers from that file and fails loudly on an unrecorded
request. Requests are keyed by model, messages, and tool names."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path

from factory.ports.context import AdapterContext
from factory.ports.errors import InvalidRequest
from factory.ports.types import Message, ModelRequest, ModelResponse, Role, ToolCall, Usage


def request_key(request: ModelRequest) -> str:
    doc = {"model": request.model, "tools": sorted(t.name for t in request.tools),
           "messages": [[m.role.value, m.content, [[c.name, json.dumps(dict(c.arguments), sort_keys=True)]
                                                   for c in m.tool_calls]] for m in request.messages]}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


class ReplayModelProvider:
    def __init__(self, path: Path, mode: str = "replay", inner=None) -> None:
        if mode not in ("replay", "record"):
            raise InvalidRequest("replay provider mode must be replay or record")
        if mode == "record" and inner is None:
            raise InvalidRequest("record mode needs settings.inner naming another provider")
        self.path = path
        self.mode = mode
        self.inner = inner
        self._lock = threading.Lock()
        self._recorded: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._recorded[entry["key"]] = entry["response"]

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> ReplayModelProvider:
        inner_name = str(settings.get("inner", ""))
        inner = None
        if inner_name:
            from factory.config.registry import resolve_class
            sel = ctx.config.models.providers[inner_name]
            inner = resolve_class(sel.adapter, ctx.config.plugins.aliases).from_settings(dict(sel.settings), ctx)
        return cls(ctx.base_dir / str(settings.get("path", "var/replay/models.jsonl")),
                   str(settings.get("mode", "replay")), inner)

    def complete(self, request: ModelRequest) -> ModelResponse:
        key = request_key(request)
        if self.mode == "replay":
            doc = self._recorded.get(key)
            if doc is None:
                raise InvalidRequest(f"no recorded response for request {key[:12]} (stage {request.context.stage})")
            calls = tuple(ToolCall(c["id"], c["name"], c["arguments"]) for c in doc["tool_calls"])
            return ModelResponse(doc["model"], Message(Role.ASSISTANT, doc["content"], calls),
                                 Usage(doc["input_tokens"], doc["output_tokens"]), doc["finish_reason"])
        resp = self.inner.complete(request)
        entry = {"key": key, "stage": request.context.stage, "response": {
            "model": resp.model, "content": resp.message.content, "finish_reason": resp.finish_reason,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": dict(c.arguments)} for c in resp.message.tool_calls],
            "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            self._recorded[key] = entry["response"]
        return resp
