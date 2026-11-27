"""Hand-written Jira Server REST v2 emulator for contract tests. Responses
follow the documented shapes; only the endpoints the adapter uses exist, and
it applies the subset of JQL the adapter emits (so a translation bug shows up
as a contract failure)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from factory.adapters.http import Response

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


class JiraEmulator:
    def __init__(self, base_url: str = "https://jira.sim.invalid", statuses=("Open", "Ready", "In Progress", "Done"),
                 transitions: dict | None = None, groups: dict | None = None, users: dict | None = None) -> None:
        self.base_url = base_url
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.links: list[dict] = []
        self.attachments: dict[str, tuple[str, bytes]] = {}
        self.statuses = statuses
        self.transitions = transitions
        self.groups = groups or {}
        self.users = users or {}  # name -> (email, active)
        self.requests: list[tuple[str, str, object]] = []
        self.auth_seen: set[str] = set()
        self._seq = 0
        self._cid = 100
        self._clock = T0

    # ---- seeding ---------------------------------------------------------------
    def seed(self, key: str, summary: str = "", status: str = "Open", labels=(), components=(), issue_type="Task",
             priority="Medium", reporter: str = "", fields: dict | None = None, project: str = "") -> None:
        self._clock += timedelta(seconds=1)
        self.issues[key] = {"id": str(len(self.issues) + 1000), "key": key, "fields": {
            "project": {"key": project or key.split("-")[0]}, "summary": summary, "description": "",
            "status": {"name": status}, "issuetype": {"name": issue_type}, "priority": {"name": priority},
            "labels": list(labels), "components": [{"name": c} for c in components],
            "reporter": {"name": reporter, "displayName": reporter} if reporter else None, "assignee": None,
            "created": _fmt(self._clock), "updated": _fmt(self._clock), "attachment": [], **(fields or {})}}
        self.comments.setdefault(key, [])

    def human_comment(self, key: str, author: str, body: str) -> None:
        self._cid += 1
        self.comments[key].append({"id": str(self._cid), "author": {"name": author, "displayName": author},
                                   "body": body, "created": _fmt(self._clock), "properties": []})

    def attach(self, key: str, name: str, data: bytes) -> str:
        aid = str(9000 + len(self.attachments))
        self.attachments[aid] = (name, data)
        self.issues[key]["fields"]["attachment"].append({"id": aid, "filename": name, "mimeType": "text/plain",
                                                          "size": len(data)})
        return aid

    # ---- JQL subset -------------------------------------------------------------
    def _matches(self, issue: dict, jql: str) -> bool:
        f = issue["fields"]
        where = jql.split(" ORDER BY ")[0].strip()
        if not where or where.startswith("ORDER BY"):
            return True
        for clause in re.split(r" AND (?![^(]*\))", where):
            clause = clause.strip()
            vals = re.findall(r'"((?:[^"\\]|\\.)*)"', clause)
            labels = set(f.get("labels") or [])
            if clause.startswith("(labels is EMPTY OR labels not in"):
                if labels & set(vals):
                    return False
            elif clause.startswith("labels in"):
                if not labels & set(vals):
                    return False
            elif clause.startswith("labels ="):
                if vals[0] not in labels:
                    return False
            elif m := re.match(r"cf\[(\d+)\] (in|~)", clause):
                value = f.get(f"customfield_{m.group(1)}")
                value = value.get("value") if isinstance(value, dict) else value
                if (m.group(2) == "in" and str(value) not in vals) or (m.group(2) == "~" and vals[0] not in str(value or "")):
                    return False
            else:
                name = clause.split()[0]
                actual = {"project": f["project"]["key"], "issuetype": f["issuetype"]["name"],
                          "priority": f["priority"]["name"], "status": f["status"]["name"]}.get(name)
                if name == "component":
                    if not {c["name"] for c in f["components"]} & set(vals):
                        return False
                elif actual is None:
                    raise ValueError(f"emulator does not understand JQL clause {clause!r}")
                elif actual not in vals:
                    return False
        return True

    # ---- transport ------------------------------------------------------------------
    def __call__(self, method: str, url: str, headers, body, timeout) -> Response:
        parts = urlsplit(url)
        assert url.startswith(self.base_url), url
        path = parts.path
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        data = json.loads(body) if body else None
        self.requests.append((method, path, data))
        self.auth_seen.add(headers.get("Authorization", "")[:6])
        self._clock += timedelta(seconds=1)
        try:
            return self._route(method, path, query, data)
        except KeyError:
            return Response(404, {}, b'{"errorMessages": ["not found"]}')

    def _ok(self, doc=None, status: int = 200) -> Response:
        return Response(status, {}, json.dumps(doc).encode() if doc is not None else b"")

    def _route(self, method, path, query, data) -> Response:
        api = "/rest/api/2"
        if method == "POST" and path == f"{api}/search":
            hits = [i for i in sorted(self.issues.values(), key=lambda i: (i["fields"]["created"], i["key"]))
                    if self._matches(i, data["jql"])]
            start, size = data.get("startAt", 0), data.get("maxResults", 50)
            return self._ok({"startAt": start, "maxResults": size, "total": len(hits), "issues": hits[start:start + size]})
        if method == "POST" and path == f"{api}/issue":
            f = data["fields"]
            project = f["project"]["key"]
            self._seq += 1
            key = f"{project}-{100 + self._seq}"
            self.seed(key, f["summary"], labels=f.get("labels", []), components=[c["name"] for c in f.get("components", [])],
                      issue_type=f["issuetype"]["name"], reporter=(f.get("reporter") or {}).get("name", ""),
                      fields={k: v for k, v in f.items() if k.startswith("customfield_")})
            self.issues[key]["fields"]["description"] = f.get("description", "")
            return self._ok({"id": self.issues[key]["id"], "key": key}, 201)
        if m := re.fullmatch(rf"{api}/issue/([A-Z]+-\d+)", path):
            issue = self.issues[m.group(1)]
            if method == "GET":
                return self._ok(issue)
            if method == "PUT":
                for k, v in (data.get("fields") or {}).items():
                    issue["fields"][k] = v
                for op in (data.get("update") or {}).get("labels", []):
                    labels = issue["fields"]["labels"]
                    if "add" in op and op["add"] not in labels:
                        labels.append(op["add"])
                    if "remove" in op and op["remove"] in labels:
                        labels.remove(op["remove"])
                return self._ok(None, 204)
        if m := re.fullmatch(rf"{api}/issue/([A-Z]+-\d+)/transitions", path):
            issue = self.issues[m.group(1)]
            current = issue["fields"]["status"]["name"]
            allowed = (self.transitions or {}).get(current, [s for s in self.statuses if s != current])
            options = [{"id": str(i + 11), "name": f"Move to {s}", "to": {"name": s}} for i, s in enumerate(allowed)]
            if method == "GET":
                return self._ok({"transitions": options})
            chosen = next((o for o in options if o["id"] == data["transition"]["id"]), None)
            if chosen is None:
                return Response(400, {}, b'{"errorMessages": ["invalid transition"]}')
            issue["fields"]["status"] = {"name": chosen["to"]["name"]}
            return self._ok(None, 204)
        if m := re.fullmatch(rf"{api}/issue/([A-Z]+-\d+)/comment", path):
            key = m.group(1)
            comments = self.comments[key]
            if method == "GET":
                start, size = int(query.get("startAt", 0)), int(query.get("maxResults", 50))
                return self._ok({"startAt": start, "total": len(comments), "comments": comments[start:start + size]})
            self._cid += 1
            c = {"id": str(self._cid), "author": {"name": "svc-factory", "displayName": "Factory"}, "body": data["body"],
                 "created": _fmt(self._clock), "properties": data.get("properties", [])}
            comments.append(c)
            return self._ok(c, 201)
        if m := re.fullmatch(rf"{api}/issue/([A-Z]+-\d+)/comment/(\d+)", path):
            c = next(c for c in self.comments[m.group(1)] if c["id"] == m.group(2))
            c["body"] = data["body"]
            c["updated"] = _fmt(self._clock)
            return self._ok(c)
        if method == "POST" and path == f"{api}/issueLink":
            self.links.append(data)
            return self._ok(None, 201)
        if method == "GET" and path == f"{api}/user/search":
            q = query["username"].lower()
            found = [{"name": n, "displayName": n, "emailAddress": e, "active": a}
                     for n, (e, a) in sorted(self.users.items()) if q in n.lower() or q in e.lower()]
            return self._ok(found[:int(query.get("maxResults", 50))])
        if method == "GET" and path == f"{api}/user":
            name = query["username"]
            return self._ok({"name": name, "groups": {"items": [{"name": g} for g in self.groups.get(name, [])]}})
        if m := re.fullmatch(rf"{api}/attachment/(\d+)", path):
            name, _ = self.attachments[m.group(1)]
            return self._ok({"id": m.group(1), "filename": name, "content": f"{self.base_url}/secure/attachment/{m.group(1)}/{name}"})
        if m := re.fullmatch(r"/secure/attachment/(\d+)/.+", path):
            return Response(200, {}, self.attachments[m.group(1)][1])
        raise KeyError(path)
