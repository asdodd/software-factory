"""Jira Server / Data Center adapter (REST API v2), written from the API
documentation and exercised only through the TicketSystem contract suite
against a fixture emulator. Never run against a live Jira in this build.

- The neutral TicketFilter becomes JQL; `native_adapter = "jira"` appends a raw
  JQL clause (validated for balanced quotes and parentheses).
- Logical field names map to custom field ids through workflow.fields.
- transition(key, status) finds the workflow transition whose target is the
  native status (or the configured transition name for it).
- Comment idempotency uses a comment property ("factory" -> {"op": key}), so a
  retried write never posts twice and bodies stay clean.
- Commenter groups are looked up (and cached) so group-based commenter rules work."""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from factory.adapters.http import HttpClient, Transport, urllib_transport
from factory.ports.context import AdapterContext
from factory.ports.errors import Conflict, InvalidRequest, NotFound
from factory.ports.types import (
    Attachment,
    Comment,
    Cursor,
    LinkKind,
    Page,
    Principal,
    PrincipalKind,
    Ticket,
    TicketDraft,
    TicketFilter,
    TicketKey,
    TicketUpdate,
)

API = "/rest/api/2"
BASE_FIELDS = ("summary", "description", "status", "issuetype", "priority", "labels", "components", "reporter",
               "assignee", "created", "updated", "attachment", "project")


def jql_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _in(field: str, values: Iterable[str]) -> str:
    return f"{field} in ({', '.join(jql_quote(v) for v in sorted(values))})"


def to_jql(f: TicketFilter, field_ids: Mapping[str, str]) -> str:
    clauses = []
    if f.labels_any:
        clauses.append(_in("labels", f.labels_any))
    for label in sorted(f.labels_all):
        clauses.append(f"labels = {jql_quote(label)}")
    if f.labels_none:
        clauses.append(f"(labels is EMPTY OR labels not in ({', '.join(jql_quote(v) for v in sorted(f.labels_none))}))")
    for field, values in (("project", f.projects), ("component", f.components), ("issuetype", f.issue_types),
                          ("priority", f.priorities), ("status", f.statuses)):
        if values:
            clauses.append(_in(field, values))
    for name, values in sorted(f.custom_fields.items()):
        fid = field_ids.get(name)
        if not fid:
            raise InvalidRequest(f"custom field {name!r} has no id in workflow.fields")
        clauses.append(_in(f"cf[{fid.removeprefix('customfield_')}]", values))
    if f.native is not None:
        clauses.append(f"({f.native.query})")
    return " AND ".join(clauses) + " ORDER BY created ASC, key ASC" if clauses else "ORDER BY created ASC, key ASC"


def parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, UTC)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


class JiraTicketSystem:
    NATIVE_ADAPTER = "jira"

    def __init__(self, http: HttpClient, field_ids: Mapping[str, str], link_types: Mapping[str, str],
                 transition_names: Mapping[str, str], statuses: Mapping[str, str], resolve_groups: bool = True) -> None:
        self.http = http
        self.field_ids = dict(field_ids)
        self.field_names = {v: k for k, v in self.field_ids.items()}
        self.link_types = dict(link_types)
        self.transition_names = {statuses[k]: v for k, v in transition_names.items() if k in statuses}
        self.resolve_groups = resolve_groups
        self._groups: dict[str, frozenset[str]] = {}
        self._by_email: dict[str, Principal | None] = {}

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext,
                      transport: Transport | None = None) -> JiraTicketSystem:
        base_url = str(settings.get("base_url", ""))
        if not base_url:
            raise InvalidRequest("plugins.ticket_system.settings.base_url is required")
        auth = str(settings.get("auth", "bearer"))
        if auth == "basic":
            user = ctx.secrets.resolve(settings["username"], "jira username")
            password = ctx.secrets.resolve(settings["password"], "jira password")
            header = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        else:
            header = "Bearer " + ctx.secrets.resolve(settings["token"], "jira token")
        transport = transport or urllib_transport(str(settings.get("ca_bundle_path", "")),
                                                  bool(settings.get("verify", True)))
        http = HttpClient(base_url, {"Authorization": header}, transport, float(settings.get("timeout_seconds", 30)),
                          int(settings.get("max_retries", 3)))
        wf = ctx.config.workflow
        return cls(http, wf.fields, wf.link_types, wf.transitions, wf.statuses, bool(settings.get("resolve_groups", True)))

    # ---- mapping ------------------------------------------------------------
    def _principal(self, doc: Mapping | None) -> Principal | None:
        if not doc:
            return None
        name = str(doc.get("name") or doc.get("key") or "")
        return Principal(name, str(doc.get("displayName", name)), PrincipalKind.USER, self._user_groups(name))

    def _user_groups(self, name: str) -> frozenset[str]:
        if not self.resolve_groups or not name:
            return frozenset()
        if name not in self._groups:
            try:
                doc = self.http.request("GET", f"{API}/user", {"username": name, "expand": "groups"}).json()
                self._groups[name] = frozenset(g["name"] for g in doc.get("groups", {}).get("items", []))
            except NotFound:
                self._groups[name] = frozenset()
        return self._groups[name]

    def find_user_by_email(self, email: str) -> Principal | None:
        """Server/Data Center: user/search matches username, name, or email, so
        results are narrowed to an exact, active email match. Cached per run."""
        email = email.strip().lower()
        if not email:
            return None
        if email not in self._by_email:
            docs = self.http.request("GET", f"{API}/user/search",
                                     {"username": email, "maxResults": "20"}).json() or []
            exact = [d for d in docs if str(d.get("emailAddress", "")).lower() == email and d.get("active", True)]
            self._by_email[email] = self._principal(exact[0]) if len(exact) == 1 else None
        return self._by_email[email]

    def _ticket(self, doc: Mapping) -> Ticket:
        f = doc["fields"]
        custom = {}
        for fid, name in self.field_names.items():
            value = f.get(fid)
            if isinstance(value, dict):
                value = value.get("value", value.get("name"))
            elif isinstance(value, list):
                value = [v.get("value", v) if isinstance(v, dict) else v for v in value]
            custom[name] = "" if value is None else value
        return Ticket(
            key=doc["key"], project=(f.get("project") or {}).get("key", doc["key"].split("-")[0]),
            summary=f.get("summary") or "", description=f.get("description") or "",
            status=(f.get("status") or {}).get("name", ""), issue_type=(f.get("issuetype") or {}).get("name", ""),
            priority=(f.get("priority") or {}).get("name", ""), labels=frozenset(f.get("labels") or []),
            components=frozenset(c["name"] for c in f.get("components") or []),
            reporter=self._principal(f.get("reporter")), assignee=self._principal(f.get("assignee")),
            created=parse_time(f.get("created")), updated=parse_time(f.get("updated")), fields=custom,
            attachments=tuple(Attachment(str(a["id"]), a["filename"], a.get("mimeType", ""), int(a.get("size", 0)))
                              for a in f.get("attachment") or []),
        )

    def _fields_out(self, fields: Mapping[str, object]) -> dict:
        out = {}
        for name, value in fields.items():
            fid = self.field_ids.get(name)
            if not fid:
                raise InvalidRequest(f"logical field {name!r} has no id in workflow.fields")
            out[fid] = value
        return out

    def _comment(self, doc: Mapping) -> Comment:
        return Comment(str(doc["id"]), self._principal(doc.get("author")) or Principal(""), doc.get("body", ""),
                       parse_time(doc.get("created")), parse_time(doc["updated"]) if doc.get("updated") else None)

    @property
    def _field_list(self) -> str:
        return ",".join((*BASE_FIELDS, *self.field_ids.values()))

    # ---- port -----------------------------------------------------------------
    def validate_filter(self, filter: TicketFilter) -> None:
        if filter.native is not None:
            if filter.native.adapter != self.NATIVE_ADAPTER:
                raise InvalidRequest(f"native query for {filter.native.adapter!r} cannot run on jira")
            q = filter.native.query
            if q.count('"') % 2 or q.count("(") != q.count(")") or "ORDER BY" in q.upper():
                raise InvalidRequest("native JQL must have balanced quotes and parentheses and no ORDER BY")
        to_jql(filter, self.field_ids)

    def search(self, filter: TicketFilter, cursor: Cursor | None = None, limit: int = 50) -> Page[Ticket]:
        self.validate_filter(filter)
        start = int(cursor or 0)
        doc = self.http.request("POST", f"{API}/search", json_body={
            "jql": to_jql(filter, self.field_ids), "startAt": start, "maxResults": limit,
            "fields": self._field_list.split(",")}).json()
        issues = doc.get("issues", [])
        nxt = start + len(issues)
        return Page(tuple(self._ticket(i) for i in issues), str(nxt) if nxt < int(doc.get("total", 0)) else None)

    def get(self, key: TicketKey) -> Ticket:
        return self._ticket(self.http.request("GET", f"{API}/issue/{key}", {"fields": self._field_list}).json())

    def create(self, draft: TicketDraft) -> Ticket:
        if draft.idempotency_key and "source_ref" in self.field_ids:
            fid = self.field_ids["source_ref"].removeprefix("customfield_")
            found = self.http.request("POST", f"{API}/search", json_body={
                "jql": f"cf[{fid}] ~ {jql_quote(draft.idempotency_key)}", "maxResults": 20,
                "fields": self._field_list.split(",")}).json()
            for issue in found.get("issues", []):
                ticket = self._ticket(issue)
                if ticket.fields.get("source_ref") == draft.idempotency_key:
                    return ticket
        fields = {"project": {"key": draft.project}, "summary": draft.summary, "description": draft.description,
                  "issuetype": {"name": draft.issue_type}, "labels": sorted(draft.labels),
                  "components": [{"name": c} for c in sorted(draft.components)],
                  **self._fields_out(draft.fields)}
        if draft.reporter and draft.reporter.id:
            fields["reporter"] = {"name": draft.reporter.id}
        created = self.http.request("POST", f"{API}/issue", json_body={"fields": fields}).json()
        return self.get(created["key"])

    def update(self, key: TicketKey, update: TicketUpdate) -> Ticket:
        fields = self._fields_out(update.fields)
        if update.summary is not None:
            fields["summary"] = update.summary
        if update.description is not None:
            fields["description"] = update.description
        if update.assignee is not None:
            fields["assignee"] = {"name": update.assignee.id} if update.assignee.id else None
        if fields:
            self.http.request("PUT", f"{API}/issue/{key}", json_body={"fields": fields})
        return self.get(key)

    def transition(self, key: TicketKey, to_status: str) -> Ticket:
        current = self.get(key)
        if current.status == to_status:
            return current
        options = self.http.request("GET", f"{API}/issue/{key}/transitions").json().get("transitions", [])
        wanted = self.transition_names.get(to_status)
        match = next((t for t in options if (wanted and t["name"] == wanted) or
                      (not wanted and (t.get("to") or {}).get("name") == to_status)), None)
        if match is None:
            raise Conflict(f"no transition from {current.status!r} to {to_status!r} on {key}")
        self.http.request("POST", f"{API}/issue/{key}/transitions", json_body={"transition": {"id": match["id"]}})
        return self.get(key)

    def _all_comments(self, key: TicketKey) -> list[dict]:
        out, start = [], 0
        while True:
            doc = self.http.request("GET", f"{API}/issue/{key}/comment", {
                "startAt": start, "maxResults": 100, "orderBy": "created", "expand": "properties"}).json()
            batch = doc.get("comments", [])
            out.extend(batch)
            start += len(batch)
            if not batch or start >= int(doc.get("total", 0)):
                return out

    def add_comment(self, key: TicketKey, body: str, idempotency_key: str) -> Comment:
        for c in self._all_comments(key):
            for prop in c.get("properties", []):
                if prop.get("key") == "factory" and (prop.get("value") or {}).get("op") == idempotency_key:
                    return self._comment(c)
        doc = self.http.request("POST", f"{API}/issue/{key}/comment", json_body={
            "body": body, "properties": [{"key": "factory", "value": {"op": idempotency_key}}]}).json()
        return self._comment(doc)

    def edit_comment(self, key: TicketKey, comment_id: str, body: str) -> Comment:
        return self._comment(self.http.request("PUT", f"{API}/issue/{key}/comment/{comment_id}",
                                               json_body={"body": body}).json())

    def list_comments(self, key: TicketKey, cursor: Cursor | None = None) -> Page[Comment]:
        after = int(cursor) if cursor and cursor.isdigit() else -1
        fresh = [self._comment(c) for c in self._all_comments(key) if int(c["id"]) > after]
        return Page(tuple(fresh), fresh[-1].id if fresh else cursor)

    def link(self, source: TicketKey, target: TicketKey, kind: LinkKind) -> None:
        name = self.link_types.get(kind.value)
        if not name:
            raise InvalidRequest(f"workflow.link_types.{kind.value} is not configured")
        self.http.request("POST", f"{API}/issueLink", json_body={
            "type": {"name": name}, "outwardIssue": {"key": source}, "inwardIssue": {"key": target}})

    def set_labels(self, key: TicketKey, add: Iterable[str] = (), remove: Iterable[str] = ()) -> Ticket:
        ops = [{"add": a} for a in add] + [{"remove": r} for r in remove]
        if ops:
            self.http.request("PUT", f"{API}/issue/{key}", json_body={"update": {"labels": ops}})
        return self.get(key)

    def get_attachment(self, key: TicketKey, attachment_id: str) -> bytes:
        meta = self.http.request("GET", f"{API}/attachment/{attachment_id}").json()
        content = str(meta["content"])
        path = content[len(self.http.base_url.rstrip("/")):] if content.startswith(self.http.base_url) else content
        return self.http.request("GET", path, extra_headers={"Accept": "*/*"}).body
