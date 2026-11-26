"""OpenAI-compatible chat completions provider, for a government endpoint or
an internal gateway. The endpoint may be request-limited with a timed
back-off: 429/503 responses are waited out (Retry-After honored, backoff
capped at max_backoff_seconds) for up to rate_limit_max_wait_seconds
(0 = no limit) instead of failing the call. Written from the published API schema and exercised only
through the ModelProvider contract suite against a fixture emulator."""

from __future__ import annotations

import json
from collections.abc import Mapping

from factory.adapters.http import HttpClient, Transport, urllib_transport
from factory.ports.context import AdapterContext
from factory.ports.errors import InvalidRequest
from factory.ports.types import Message, ModelRequest, ModelResponse, Role, ToolCall, Usage


def to_wire(m: Message) -> dict:
    out: dict = {"role": m.role.value, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [{"id": c.id, "type": "function",
                              "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))}}
                             for c in m.tool_calls]
        out["content"] = m.content or None
    if m.role is Role.TOOL:
        out["tool_call_id"] = m.tool_call_id or ""
    return out


class OpenAICompatibleProvider:
    def __init__(self, http: HttpClient, extra_body: Mapping[str, object] | None = None) -> None:
        self.http = http
        self.extra_body = dict(extra_body or {})

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext,
                      transport: Transport | None = None) -> OpenAICompatibleProvider:
        base_url = str(settings.get("base_url", ""))
        if not base_url:
            raise InvalidRequest("models.providers.<name>.settings.base_url is required")
        key = ctx.secrets.resolve(settings["api_key"], "model api key")
        headers = {"Authorization": f"Bearer {key}",
                   **{str(k): str(v) for k, v in dict(settings.get("headers", {})).items()}}
        transport = transport or urllib_transport(str(settings.get("ca_bundle_path", "")), bool(settings.get("verify", True)))
        http = HttpClient(base_url, headers, transport, float(settings.get("timeout_seconds", 300)),
                          int(settings.get("max_retries", 3)), patient=True,
                          patient_max_wait_seconds=float(settings.get("rate_limit_max_wait_seconds", 0)),
                          max_backoff_seconds=float(settings.get("max_backoff_seconds", 60)))
        return cls(http, dict(settings.get("extra_body", {})))

    def complete(self, request: ModelRequest) -> ModelResponse:
        body: dict = {"model": request.model, "messages": [to_wire(m) for m in request.messages],
                      "max_tokens": request.max_output_tokens, "temperature": request.temperature, **self.extra_body}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": dict(t.json_schema)}}
                             for t in request.tools]
        doc = self.http.request("POST", "/chat/completions", json_body=body).json()
        choice = doc["choices"][0]
        msg = choice.get("message") or {}
        calls = tuple(ToolCall(c.get("id", ""), c["function"]["name"], json.loads(c["function"].get("arguments") or "{}"))
                      for c in msg.get("tool_calls") or [])
        usage = doc.get("usage") or {}
        return ModelResponse(doc.get("model", request.model), Message(Role.ASSISTANT, msg.get("content") or "", calls),
                             Usage(int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))),
                             choice.get("finish_reason") or "stop")
