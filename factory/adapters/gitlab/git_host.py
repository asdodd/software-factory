"""GitLab adapter: REST API v4 for merge requests, discussions, suggestions,
and comment events; the git executable (fixed argv, timeouts, credential
helper) for clone, pull, branch, and push. Written from the API documentation
and exercised only through the GitHost contract suite against a fixture
emulator and local repositories. Never run against a live GitLab in this build.

Note idempotency uses an invisible marker (<!-- factory-op:KEY -->) appended
to each note; before posting, the target thread is checked for it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from factory.adapters.http import HttpClient, Transport, urllib_transport
from factory.config.secrets import SecretError
from factory.ports.context import AdapterContext
from factory.ports.errors import Conflict, InvalidRequest, NotFound
from factory.ports.types import (
    CheckoutResult,
    Cursor,
    HostCommentEvent,
    MergeRequest,
    MergeRequestDraft,
    MergeRequestState,
    MrComment,
    Page,
    Principal,
    PrincipalKind,
    RepoRef,
    Suggestion,
)
from factory.vcs.git import Git, GitError, HostAuth, options_from_config

STATES = {"opened": MergeRequestState.OPEN, "merged": MergeRequestState.MERGED, "closed": MergeRequestState.CLOSED,
          "locked": MergeRequestState.OPEN}


def marker(key: str) -> str:
    return f"\n\n<!-- factory-op:{key} -->"


def _pid(path: str) -> str:
    return quote(path, safe="")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitlabGitHost:
    def __init__(self, http: HttpClient, git: Git, base_url: str, clone_base_url: str,
                 watched_projects: list[str], identity: str) -> None:
        self.http = http
        self.git = git
        self.base_url = base_url.rstrip("/")
        self.clone_base_url = clone_base_url.rstrip("/")
        self.watched_projects = watched_projects
        self.identity = identity

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext,
                      transport: Transport | None = None) -> GitlabGitHost:
        base_url = str(settings.get("base_url", "")).rstrip("/")
        if not base_url:
            raise InvalidRequest("plugins.git_host.settings.base_url is required")
        token = ctx.secrets.resolve(settings["token"], "gitlab token")
        api_url = str(settings.get("api_url") or f"{base_url}/api/v4")
        clone_base = str(settings.get("clone_base_url") or base_url)
        auth = {}
        host = urlsplit(clone_base).hostname
        if host:
            auth[host] = HostAuth("oauth2", token)
        for h, cred in ctx.config.git.host_credentials.items():
            try:
                user = ctx.secrets.resolve(cred.username, "username") if (cred.username.env or cred.username.file) else "oauth2"
                auth[h] = HostAuth(user, ctx.secrets.resolve(cred.token, f"git.host_credentials.{h}.token"))
            except SecretError:
                continue
        identity = str(settings.get("identity", "factory"))
        git = Git(options_from_config(ctx.config, auth, allow_protocols=str(settings.get("allow_protocols", "https:ssh")),
                                      author_name=identity, author_email=str(settings.get("author_email",
                                                                                         f"{identity}@factory.invalid"))),
                  ctx.redactor)
        transport = transport or urllib_transport(str(settings.get("ca_bundle_path", "")), bool(settings.get("verify", True)))
        http = HttpClient(api_url, {"PRIVATE-TOKEN": token}, transport, float(settings.get("timeout_seconds", 30)),
                          int(settings.get("max_retries", 3)))
        return cls(http, git, base_url, clone_base, [str(p) for p in settings.get("watched_projects", [])], identity)

    # ---- git ---------------------------------------------------------------------
    def url(self, path: str) -> str:
        return f"{self.clone_base_url}/{path}.git"

    def clone(self, repo: RepoRef, dest: Path, ref: str | None = None) -> CheckoutResult:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self.git.clone(self.url(repo.path), dest, ref or repo.default_branch)
        except GitError as exc:
            raise NotFound(f"repo {repo.path}: {exc}") from None

    def pull(self, workspace: Path) -> CheckoutResult:
        return self.git.pull(workspace)

    def create_branch(self, workspace: Path, name: str, from_ref: str) -> None:
        self.git.run(["checkout", "-q", "-B", name, from_ref], cwd=workspace)

    def push_branch(self, workspace: Path, branch: str) -> str:
        proc = self.git.run(["push", "-q", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], cwd=workspace,
                            check=False)
        if proc.returncode != 0:
            raise Conflict(f"push of {branch} rejected (non-fast-forward or protected)")
        return self.git.head(workspace)

    def compare_url(self, repo: RepoRef, base: str, head: str) -> str | None:
        return f"{self.base_url}/{repo.path}/-/compare/{quote(base, safe='')}...{quote(head, safe='')}"

    # ---- merge requests ----------------------------------------------------------------
    def _mr(self, repo: RepoRef, doc: Mapping) -> MergeRequest:
        return MergeRequest(repo, str(doc["iid"]), doc.get("web_url", ""), doc["source_branch"], doc["target_branch"],
                            STATES.get(doc.get("state", "opened"), MergeRequestState.OPEN))

    def open_merge_request(self, draft: MergeRequestDraft) -> MergeRequest:
        pid = _pid(draft.repo.path)
        existing = self.http.request("GET", f"/projects/{pid}/merge_requests",
                                     {"source_branch": draft.source_branch, "state": "opened"}).json()
        if existing:
            return self._mr(draft.repo, existing[0])
        doc = self.http.request("POST", f"/projects/{pid}/merge_requests", json_body={
            "source_branch": draft.source_branch, "target_branch": draft.target_branch, "title": draft.title,
            "description": draft.description + marker(draft.idempotency_key), "remove_source_branch": False}).json()
        return self._mr(draft.repo, doc)

    def get_merge_request(self, repo: RepoRef, mr_id: str) -> MergeRequest:
        return self._mr(repo, self.http.request("GET", f"/projects/{_pid(repo.path)}/merge_requests/{mr_id}").json())

    def _discussions(self, repo: RepoRef, mr_id: str) -> list[dict]:
        out, page = [], 1
        while True:
            batch = self.http.request("GET", f"/projects/{_pid(repo.path)}/merge_requests/{mr_id}/discussions",
                                      {"per_page": 100, "page": page}).json()
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def _principal(self, doc: Mapping) -> Principal:
        kind = PrincipalKind.BOT if doc.get("bot") else PrincipalKind.USER
        return Principal(str(doc.get("username", "")), str(doc.get("name", "")), kind)

    def list_mr_comments(self, repo: RepoRef, mr_id: str, cursor: Cursor | None = None) -> Page[MrComment]:
        after = int(cursor) if cursor and cursor.isdigit() else -1
        notes = []
        for d in self._discussions(repo, mr_id):
            for n in d.get("notes", []):
                if n.get("system") or int(n["id"]) <= after:
                    continue
                pos = n.get("position") or {}
                notes.append(MrComment(str(n["id"]), str(d["id"]), self._principal(n.get("author", {})),
                                       n.get("body", ""), _time(n["created_at"]), pos.get("new_path"),
                                       pos.get("new_line")))
        notes.sort(key=lambda c: int(c.id))
        return Page(tuple(notes), notes[-1].id if notes else cursor)

    def _already_posted(self, notes: list[dict], key: str) -> dict | None:
        tag = marker(key).strip()
        return next((n for n in notes if tag in n.get("body", "")), None)

    def reply_in_thread(self, repo: RepoRef, mr_id: str, thread_id: str, body: str, idempotency_key: str) -> MrComment:
        base = f"/projects/{_pid(repo.path)}/merge_requests/{mr_id}/discussions/{thread_id}"
        current = self.http.request("GET", base).json()
        note = self._already_posted(current.get("notes", []), idempotency_key)
        if note is None:
            note = self.http.request("POST", f"{base}/notes", json_body={"body": body + marker(idempotency_key)}).json()
        return MrComment(str(note["id"]), thread_id, self._principal(note.get("author", {})), body,
                         _time(note["created_at"]))

    def post_suggestion(self, repo: RepoRef, mr_id: str, suggestion: Suggestion, idempotency_key: str) -> MrComment:
        pid = _pid(repo.path)
        for d in self._discussions(repo, mr_id):
            note = self._already_posted(d.get("notes", []), idempotency_key)
            if note:
                return MrComment(str(note["id"]), str(d["id"]), self._principal(note.get("author", {})),
                                 note["body"], _time(note["created_at"]))
        versions = self.http.request("GET", f"/projects/{pid}/merge_requests/{mr_id}/versions").json()
        if not versions:
            raise Conflict(f"merge request {mr_id} has no diff versions yet")
        v = versions[0]
        span = suggestion.line_end - suggestion.line_start
        body = f"{suggestion.note}\n\n```suggestion:-{span}+0\n{suggestion.replacement}\n```" + marker(idempotency_key)
        d = self.http.request("POST", f"/projects/{pid}/merge_requests/{mr_id}/discussions", json_body={
            "body": body, "position": {"position_type": "text", "base_sha": v["base_commit_sha"],
                                       "head_sha": v["head_commit_sha"], "start_sha": v["start_commit_sha"],
                                       "new_path": suggestion.path, "old_path": suggestion.path,
                                       "new_line": suggestion.line_end}}).json()
        n = d["notes"][0]
        return MrComment(str(n["id"]), str(d["id"]), self._principal(n.get("author", {})), n["body"],
                         _time(n["created_at"]), suggestion.path, suggestion.line_end)

    # ---- comment events and replies -----------------------------------------------------
    def comment_on(self, event: HostCommentEvent, body: str, idempotency_key: str) -> None:
        kind = "issues" if event.target_kind == "issue" else "merge_requests"
        path = f"/projects/{_pid(event.repo.path)}/{kind}/{event.target_id}/notes"
        existing = self.http.request("GET", path, {"per_page": 100, "sort": "desc"}).json()
        if self._already_posted(existing, idempotency_key) is None:
            self.http.request("POST", path, json_body={"body": body + marker(idempotency_key)})

    def poll_comment_events(self, cursor: Cursor | None = None) -> Page[HostCommentEvent]:
        last = json.loads(cursor) if cursor else {}
        events = []
        for project in self.watched_projects:
            after = int(last.get(project, 0))
            docs = self.http.request("GET", f"/projects/{_pid(project)}/events",
                                     {"action": "commented", "sort": "asc", "per_page": 100}).json()
            for e in docs:
                if int(e["id"]) <= after:
                    continue
                note = e.get("note") or {}
                kind = "issue" if note.get("noteable_type") == "Issue" else "merge_request"
                iid = str(note.get("noteable_iid", ""))
                section = "issues" if kind == "issue" else "merge_requests"
                events.append(HostCommentEvent(
                    str(e["id"]), RepoRef("", project, "main"), kind, iid, self._principal(e.get("author", {})),
                    note.get("body", ""), _time(e["created_at"]),
                    f"{self.base_url}/{project}/-/{section}/{iid}#note_{note.get('id', '')}"))
                last[project] = max(int(last.get(project, 0)), int(e["id"]))
        return Page(tuple(events), json.dumps(last, sort_keys=True) if last else cursor)
