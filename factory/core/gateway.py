"""Per-run loopback model gateway.

Harnesses (builtin or bring-your-own) get a 127.0.0.1 URL and a per-run token,
never upstream credentials. Requests are translated to the neutral
ModelRequest and sent through BudgetedModels, so budgets, attribution,
accounting, and trace capture apply to every harness. The stage's configured
model is always used, whatever model name the client asks for.

Dialects: openai_chat (/v1/chat/completions) and anthropic_messages
(/v1/messages), both with optional server-sent-event streaming."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from factory.ports.errors import BudgetExceeded
from factory.ports.types import CallContext, Message, ModelResponse, Role, ToolCall, ToolSpec

log = logging.getLogger("factory.gateway")


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") in ("text", "input_text"):
                parts.append(str(block.get("text", "")))
            elif isinstance(block, Mapping) and block.get("type") == "tool_result":
                parts.append(_text(block.get("content", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return "" if content is None else str(content)


def openai_to_neutral(body: Mapping) -> tuple[tuple[Message, ...], tuple[ToolSpec, ...], int]:
    messages = []
    for m in body.get("messages", []):
        role = {"system": Role.SYSTEM, "developer": Role.SYSTEM, "user": Role.USER,
                "assistant": Role.ASSISTANT, "tool": Role.TOOL}.get(m.get("role"), Role.USER)
        calls = tuple(
            ToolCall(c.get("id", ""), c["function"]["name"], json.loads(c["function"].get("arguments") or "{}"))
            for c in m.get("tool_calls") or []
        )
        messages.append(Message(role, _text(m.get("content")), calls, m.get("tool_call_id")))
    tools = tuple(
        ToolSpec(t["function"]["name"], t["function"].get("description", ""), t["function"].get("parameters", {}))
        for t in body.get("tools") or [] if t.get("type", "function") == "function"
    )
    max_tokens = int(body.get("max_completion_tokens") or body.get("max_tokens") or 4096)
    return tuple(messages), tools, max_tokens


def neutral_to_openai(resp: ModelResponse, requested_model: str) -> dict:
    msg: dict = {"role": "assistant", "content": resp.message.content or None}
    if resp.message.tool_calls:
        msg["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))}}
            for c in resp.message.tool_calls
        ]
    finish = "tool_calls" if resp.message.tool_calls else "stop"
    return {
        "id": f"chatcmpl-{secrets.token_hex(6)}", "object": "chat.completion", "model": requested_model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": resp.usage.input_tokens, "completion_tokens": resp.usage.output_tokens,
                  "total_tokens": resp.usage.total},
    }


def openai_stream(doc: dict) -> list[str]:
    choice = doc["choices"][0]
    delta: dict = {"role": "assistant"}
    if choice["message"].get("content"):
        delta["content"] = choice["message"]["content"]
    if choice["message"].get("tool_calls"):
        delta["tool_calls"] = [{**c, "index": i} for i, c in enumerate(choice["message"]["tool_calls"])]
    base = {"id": doc["id"], "object": "chat.completion.chunk", "model": doc["model"]}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}], "usage": doc["usage"]},
    ]
    return [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]


def anthropic_to_neutral(body: Mapping) -> tuple[tuple[Message, ...], tuple[ToolSpec, ...], int]:
    messages = []
    if body.get("system"):
        messages.append(Message(Role.SYSTEM, _text(body["system"])))
    for m in body.get("messages", []):
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": _text(content)}]
        if m.get("role") == "assistant":
            calls = tuple(ToolCall(b.get("id", ""), b["name"], dict(b.get("input") or {}))
                          for b in blocks if isinstance(b, Mapping) and b.get("type") == "tool_use")
            text = "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, Mapping) and b.get("type") == "text")
            messages.append(Message(Role.ASSISTANT, text, calls))
            continue
        text_parts = []
        for b in blocks:
            if isinstance(b, Mapping) and b.get("type") == "tool_result":
                messages.append(Message(Role.TOOL, _text(b.get("content", "")), (), b.get("tool_use_id")))
            else:
                text_parts.append(_text([b]))
        if any(text_parts):
            messages.append(Message(Role.USER, "\n".join(p for p in text_parts if p)))
    tools = tuple(ToolSpec(t["name"], t.get("description", ""), t.get("input_schema", {}))
                  for t in body.get("tools") or [] if "name" in t)
    return tuple(messages), tools, int(body.get("max_tokens") or 4096)


def neutral_to_anthropic(resp: ModelResponse, requested_model: str) -> dict:
    content: list[dict] = []
    if resp.message.content:
        content.append({"type": "text", "text": resp.message.content})
    for c in resp.message.tool_calls:
        content.append({"type": "tool_use", "id": c.id or f"toolu_{secrets.token_hex(6)}", "name": c.name,
                        "input": dict(c.arguments)})
    return {
        "id": f"msg_{secrets.token_hex(8)}", "type": "message", "role": "assistant", "model": requested_model,
        "content": content, "stop_reason": "tool_use" if resp.message.tool_calls else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
    }


def anthropic_stream(doc: dict) -> list[str]:
    def ev(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    start = {**doc, "content": [], "stop_reason": None, "usage": {"input_tokens": doc["usage"]["input_tokens"], "output_tokens": 0}}
    out = [ev("message_start", {"type": "message_start", "message": start})]
    for i, block in enumerate(doc["content"]):
        if block["type"] == "text":
            out.append(ev("content_block_start", {"type": "content_block_start", "index": i,
                                                  "content_block": {"type": "text", "text": ""}}))
            out.append(ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                  "delta": {"type": "text_delta", "text": block["text"]}}))
        else:
            out.append(ev("content_block_start", {"type": "content_block_start", "index": i,
                                                  "content_block": {**block, "input": {}}}))
            out.append(ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                                  "delta": {"type": "input_json_delta",
                                                            "partial_json": json.dumps(block["input"])}}))
        out.append(ev("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(ev("message_delta", {"type": "message_delta",
                                    "delta": {"stop_reason": doc["stop_reason"], "stop_sequence": None},
                                    "usage": {"output_tokens": doc["usage"]["output_tokens"]}}))
    out.append(ev("message_stop", {"type": "message_stop"}))
    return out


class Gateway:
    def __init__(self, models, provider: str, model: str, context: CallContext, bind_host: str = "127.0.0.1",
                 dialects: tuple[str, ...] = ("openai_chat", "anthropic_messages")) -> None:
        self.models = models
        self.provider = provider
        self.model = model
        self.context = context
        self.bind_host = bind_host
        self.dialects = tuple(dialects)
        self.token = secrets.token_urlsafe(24)
        self.budget_exceeded: str | None = None
        self.calls = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> Gateway:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: object) -> None:
                log.debug("gateway: " + fmt, *args)

            def _send(self, status: int, doc: dict) -> None:
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, events: list[str]) -> None:
                data = "".join(events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                supplied = self.headers.get("x-api-key", "")
                auth = self.headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    supplied = auth[7:].strip()
                return hmac.compare_digest(supplied.encode(), gateway.token.encode())

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return self._send(401, {"error": {"message": "invalid token"}})
                if self.path.rstrip("/").endswith("/models"):
                    return self._send(200, {"object": "list", "data": [{"id": gateway.model, "object": "model"}],
                                            "has_more": False})
                self._send(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return self._send(401, {"error": {"type": "authentication_error", "message": "invalid token"}})
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, {"error": {"message": "invalid JSON"}})
                path = self.path.split("?", 1)[0].rstrip("/")
                if path.endswith("/messages/count_tokens") and "anthropic_messages" in gateway.dialects:
                    messages, _, _ = anthropic_to_neutral(body)
                    return self._send(200, {"input_tokens": sum(len(m.content) for m in messages) // 4 + 1})
                if path.endswith("/chat/completions") and "openai_chat" in gateway.dialects:
                    dialect = "openai_chat"
                    messages, tools, max_tokens = openai_to_neutral(body)
                elif path.endswith("/messages") and "anthropic_messages" in gateway.dialects:
                    dialect = "anthropic_messages"
                    messages, tools, max_tokens = anthropic_to_neutral(body)
                else:
                    return self._send(404, {"error": {"message": f"unsupported endpoint {path}"}})
                try:
                    resp = gateway.complete(messages, tools, max_tokens)
                except BudgetExceeded as exc:
                    gateway.budget_exceeded = str(exc)
                    err = {"type": "rate_limit_error", "message": f"budget exceeded: {exc}"}
                    return self._send(429, {"error": err} if dialect == "openai_chat" else {"type": "error", "error": err})
                except Exception as exc:  # noqa: BLE001 - surface provider errors to the harness as 502
                    log.warning("gateway upstream error: %s", type(exc).__name__)
                    return self._send(502, {"error": {"type": "api_error", "message": "upstream model error"}})
                requested = str(body.get("model") or gateway.model)
                if dialect == "openai_chat":
                    doc = neutral_to_openai(resp, requested)
                    return self._stream(openai_stream(doc)) if body.get("stream") else self._send(200, doc)
                doc = neutral_to_anthropic(resp, requested)
                return self._stream(anthropic_stream(doc)) if body.get("stream") else self._send(200, doc)

        self._server = ThreadingHTTPServer((self.bind_host, 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="factory-gateway", daemon=True)
        self._thread.start()

    def complete(self, messages: tuple[Message, ...], tools: tuple[ToolSpec, ...], max_tokens: int) -> ModelResponse:
        self.calls += 1
        return self.models.complete(self.provider, self.model, messages, self.context, tools, max_tokens)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
