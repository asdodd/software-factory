"""Small HTTP client shared by the real adapters: JSON in and out, explicit
timeouts, a configured CA bundle, bounded retries on 429/5xx honoring
Retry-After (or, in patient mode for request-limited endpoints, waiting out
429/503 back-offs for as long as configured), and errors mapped to port errors. Secrets never appear in error
messages. The transport is injectable so contract tests run against fixtures;
the default transport (urllib) is never used in this build's tests."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode

from factory.ports.errors import Conflict, InvalidRequest, NotFound, PortError, RateLimited, Unavailable


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body or b"null")


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], Response]


def urllib_transport(ca_bundle: str = "", verify: bool = True) -> Transport:
    context = ssl.create_default_context(cafile=ca_bundle or None)
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))

    def send(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> Response:
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with opener.open(req, timeout=timeout) as resp:
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, dict(exc.headers or {}), exc.read() or b"")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise Unavailable(f"{method} {url.split('?')[0]}: {type(exc).__name__}") from None

    return send


@dataclass
class HttpClient:
    base_url: str
    headers: Mapping[str, str]
    transport: Transport
    timeout_seconds: float = 30.0
    max_retries: int = 3
    sleep: Callable[[float], None] = time.sleep
    calls: list[tuple[str, str]] = field(default_factory=list)
    patient: bool = False  # on 429/503, keep waiting out the back-off instead of giving up
    patient_max_wait_seconds: float = 0.0  # 0 = no limit
    max_backoff_seconds: float = 60.0

    def request(self, method: str, path: str, query: Mapping[str, object] | None = None, json_body: object = None,
                raw: bytes | None = None, extra_headers: Mapping[str, str] | None = None,
                ok: tuple[int, ...] = ()) -> Response:
        url = self.base_url.rstrip("/") + path
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None}, doseq=True)
        headers = {"Accept": "application/json", **self.headers, **(extra_headers or {})}
        body = raw
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        attempt = 0
        waited = 0.0
        while True:
            self.calls.append((method, path))
            resp = self.transport(method, url, headers, body, self.timeout_seconds)
            throttled = resp.status in (429, 503)
            retry = resp.status in (429, 502, 503, 504) and attempt < self.max_retries
            if throttled and self.patient:
                retry = not self.patient_max_wait_seconds or waited < self.patient_max_wait_seconds
            if not retry:
                break
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            delay = float(retry_after) if retry_after and str(retry_after).isdigit() else 0.5 * 2 ** min(attempt, 10)
            delay = min(delay, self.max_backoff_seconds)
            self.sleep(delay)
            waited += delay
            attempt += 1
        if 200 <= resp.status < 300 or resp.status in ok:
            return resp
        where = f"{method} {path}"
        if resp.status == 404:
            raise NotFound(where)
        if resp.status in (409, 412):
            raise Conflict(f"{where}: {resp.status}")
        if resp.status == 429:
            raise RateLimited(where)
        if resp.status in (400, 422):
            raise InvalidRequest(f"{where}: {_message(resp)}")
        if resp.status in (401, 403):
            raise PortError(f"{where}: not authorized ({resp.status}); check the credential and its permissions")
        raise Unavailable(f"{where}: HTTP {resp.status}")


def _message(resp: Response) -> str:
    try:
        doc = resp.json()
    except ValueError:
        return ""
    if isinstance(doc, dict):
        msgs = doc.get("errorMessages") or doc.get("message") or doc.get("error") or doc.get("errors") or ""
        return json.dumps(msgs)[:300]
    return ""
