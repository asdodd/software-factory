"""Deterministic model provider. Responses are queued per stage (and
optionally per ticket); an unscripted call is an error, never a guess."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from factory.ports.context import AdapterContext
from factory.ports.errors import InvalidRequest
from factory.ports.types import Message, ModelRequest, ModelResponse, Role, ToolCall, Usage


@dataclass
class Script:
    stage: str
    content: str = ""
    ticket: str = ""
    tool_calls: Sequence[Mapping[str, object]] = ()
    input_tokens: int = 100
    output_tokens: int = 50
    repeat: bool = False
    used: int = field(default=0)


class ScriptedModelProvider:
    def __init__(self) -> None:
        self.scripts: list[Script] = []
        self.requests: list[ModelRequest] = []

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> ScriptedModelProvider:
        return cls()

    def queue(self, stage: str, content: str | Mapping | None = None, ticket: str = "",
              tool_calls: Sequence[Mapping[str, object]] = (), input_tokens: int = 100, output_tokens: int = 50,
              repeat: bool = False) -> None:
        text = json.dumps(content) if isinstance(content, Mapping) else (content or "")
        self.scripts.append(Script(stage, text, ticket, tuple(tool_calls), input_tokens, output_tokens, repeat))

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        ctx = request.context
        specific = [s for s in self.scripts if s.ticket == ctx.ticket_key]
        generic = [s for s in self.scripts if not s.ticket]
        for script in (*specific, *generic):
            if script.stage != ctx.stage:
                continue
            if script.used and not script.repeat:
                continue
            script.used += 1
            calls = tuple(
                ToolCall(str(c.get("id", f"call-{len(self.requests)}-{i}")), str(c["name"]), dict(c.get("arguments", {})))
                for i, c in enumerate(script.tool_calls)
            )
            return ModelResponse(request.model, Message(Role.ASSISTANT, script.content, calls),
                                 Usage(script.input_tokens, script.output_tokens),
                                 "tool_calls" if calls else "stop")
        raise InvalidRequest(f"no scripted response for stage {ctx.stage!r} ticket {ctx.ticket_key!r}")
