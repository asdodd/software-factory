"""OpenAI chat-completions emulator: answers from a queue of canned replies in
the documented response shape and records the request bodies."""

from __future__ import annotations

import json

from factory.adapters.http import Response


class OpenAIEmulator:
    def __init__(self, base_url: str = "https://models.sim.invalid/v1") -> None:
        self.base_url = base_url
        self.replies: list[dict] = []
        self.bodies: list[dict] = []
        self.auth: list[str] = []
        self.fail_with: list[int] = []

    def reply(self, content: str = "", tool_calls=(), prompt_tokens=11, completion_tokens=7) -> None:
        msg = {"role": "assistant", "content": content or None}
        if tool_calls:
            msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                                  "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(tool_calls)]
        self.replies.append({"id": "chatcmpl-1", "object": "chat.completion", "model": "served-model",
                             "choices": [{"index": 0, "message": msg,
                                          "finish_reason": "tool_calls" if tool_calls else "stop"}],
                             "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}})

    def __call__(self, method, url, headers, body, timeout) -> Response:
        assert method == "POST" and url == f"{self.base_url}/chat/completions", url
        self.auth.append(headers.get("Authorization", ""))
        self.bodies.append(json.loads(body))
        if self.fail_with:
            return Response(self.fail_with.pop(0), {"Retry-After": "0"}, b'{"error": {"message": "busy"}}')
        return Response(200, {}, json.dumps(self.replies.pop(0)).encode())
