"""Hand-written GitLab REST v4 emulator for contract tests (merge requests,
discussions and notes, versions, issue notes, project events)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

from factory.adapters.http import Response

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


class GitlabEmulator:
    def __init__(self, api_url: str = "https://gitlab.sim.invalid/api/v4") -> None:
        self.api_url = api_url
        self.mrs: dict[str, dict[int, dict]] = {}
        self.discussions: dict[tuple[str, int], list[dict]] = {}
        self.target_notes: dict[tuple[str, str, int], list[dict]] = {}
        self.events: dict[str, list[dict]] = {}
        self.tokens: set[str] = set()
        self.requests: list[tuple[str, str, object]] = []
        self._id = 500
        self._clock = T0

    def _next(self) -> int:
        self._id += 1
        self._clock += timedelta(seconds=1)
        return self._id

    def _note(self, author: str, body: str, bot: bool = False, position=None) -> dict:
        return {"id": self._next(), "body": body, "author": {"username": author, "name": author, "bot": bot},
                "created_at": self._clock.isoformat().replace("+00:00", "Z"), "system": False, "position": position}

    # ---- seeding -----------------------------------------------------------------
    def reviewer_comment(self, project: str, iid: int, author: str, body: str, thread: str | None = None) -> str:
        discussions = self.discussions.setdefault((project, iid), [])
        if thread:
            d = next(d for d in discussions if d["id"] == thread)
        else:
            d = {"id": f"d{self._next()}", "notes": []}
            discussions.append(d)
        d["notes"].append(self._note(author, body))
        return d["id"]

    def system_note(self, project: str, iid: int) -> None:
        n = self._note("gitlab", "changed the description")
        n["system"] = True
        self.discussions.setdefault((project, iid), []).append({"id": f"d{self._next()}", "notes": [n]})

    def comment_event(self, project: str, author: str, body: str, noteable: str = "MergeRequest", iid: int = 1,
                      bot: bool = False) -> int:
        eid = self._next()
        self.events.setdefault(project, []).append({
            "id": eid, "action_name": "commented on", "author": {"username": author, "bot": bot},
            "created_at": self._clock.isoformat().replace("+00:00", "Z"),
            "note": {"id": eid + 10000, "body": body, "noteable_type": noteable, "noteable_iid": iid}})
        return eid

    def set_state(self, project: str, iid: int, state: str) -> None:
        self.mrs[project][iid]["state"] = state

    # ---- transport --------------------------------------------------------------------
    def __call__(self, method, url, headers, body, timeout) -> Response:
        assert url.startswith(self.api_url), url
        parts = urlsplit(url)
        path = parts.path[len(urlsplit(self.api_url).path):]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        data = json.loads(body) if body else None
        self.requests.append((method, path, data))
        self.tokens.add(headers.get("PRIVATE-TOKEN", ""))
        try:
            return self._route(method, path, query, data)
        except (KeyError, StopIteration):
            return Response(404, {}, b'{"message": "404 Not found"}')

    def _ok(self, doc, status=200) -> Response:
        return Response(status, {}, json.dumps(doc).encode())

    def _route(self, method, path, query, data) -> Response:
        m = re.fullmatch(r"/projects/([^/]+)(/.*)", path)
        project = unquote(m.group(1))
        rest = m.group(2)
        mrs = self.mrs.setdefault(project, {})
        if rest == "/merge_requests" and method == "GET":
            hits = [mr for mr in mrs.values() if mr["source_branch"] == query.get("source_branch")
                    and (query.get("state") in (None, mr["state"]))]
            return self._ok(hits)
        if rest == "/merge_requests" and method == "POST":
            iid = len(mrs) + 1
            mrs[iid] = {"iid": iid, "state": "opened", "source_branch": data["source_branch"],
                        "target_branch": data["target_branch"], "title": data["title"],
                        "description": data["description"], "web_url": f"https://gitlab.sim.invalid/{project}/-/merge_requests/{iid}"}
            return self._ok(mrs[iid], 201)
        if mm := re.fullmatch(r"/merge_requests/(\d+)", rest):
            return self._ok(mrs[int(mm.group(1))])
        if mm := re.fullmatch(r"/merge_requests/(\d+)/discussions", rest):
            iid = int(mm.group(1))
            discussions = self.discussions.setdefault((project, iid), [])
            if method == "GET":
                page, per = int(query.get("page", 1)), int(query.get("per_page", 20))
                return self._ok(discussions[(page - 1) * per: page * per])
            d = {"id": f"d{self._next()}", "notes": [self._note("svc-factory", data["body"], True, data.get("position"))]}
            discussions.append(d)
            return self._ok(d, 201)
        if mm := re.fullmatch(r"/merge_requests/(\d+)/discussions/([^/]+)(/notes)?", rest):
            d = next(d for d in self.discussions[(project, int(mm.group(1)))] if d["id"] == mm.group(2))
            if method == "GET":
                return self._ok(d)
            note = self._note("svc-factory", data["body"], True)
            d["notes"].append(note)
            return self._ok(note, 201)
        if mm := re.fullmatch(r"/merge_requests/(\d+)/versions", rest):
            return self._ok([{"id": 1, "base_commit_sha": "a" * 40, "head_commit_sha": "b" * 40,
                              "start_commit_sha": "a" * 40}])
        if mm := re.fullmatch(r"/(issues|merge_requests)/(\d+)/notes", rest):
            notes = self.target_notes.setdefault((project, mm.group(1), int(mm.group(2))), [])
            if method == "GET":
                return self._ok(list(reversed(notes)))
            notes.append(self._note("svc-factory", data["body"], True))
            return self._ok(notes[-1], 201)
        if rest == "/events":
            return self._ok(self.events.get(project, []))
        raise KeyError(rest)
