"""Minimal OpenAI-chat client for the loopback gateway. Proxies are disabled
on purpose: the gateway is local and nothing else may be reached."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class GatewayError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"gateway returned {status}: {message}")
        self.status = status


class GatewayClient:
    def __init__(self, base_url: str, token: str, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def chat(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", {}).get("message", "")
            except (json.JSONDecodeError, AttributeError):
                message = ""
            raise GatewayError(exc.code, message) from None
