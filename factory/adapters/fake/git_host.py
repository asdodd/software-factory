"""Fake git host: real git against local bare repositories, plus in-memory
merge requests and comments. No network: only the file protocol is allowed."""

from __future__ import annotations

import dataclasses
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

from factory.config.secrets import SecretError
from factory.ports.context import AdapterContext
from factory.ports.errors import Conflict, NotFound, SubmoduleFailure
from factory.ports.types import (
    CheckoutResult, Cursor, HostCommentEvent, MergeRequest, MergeRequestDraft, MergeRequestState, MrComment, Page,
    Principal, PrincipalKind, RepoRef, Suggestion,
)
from factory.vcs.git import Git, GitError, HostAuth, options_from_config


class FakeGitHost:
    def __init__(self, repos_root: Path, git: Git, clock, identity: str = "svc-factory",
                 requires_credentials: Iterable[str] = (), available_credentials: Iterable[str] = ()) -> None:
        self.repos_root = repos_root
        self.clock = clock
        self.git = git
        self.identity = Principal(identity, identity, PrincipalKind.SERVICE)
        self.requires_credentials = set(requires_credentials)
        self.available_credentials = set(available_credentials)
        self.mrs: dict[str, MergeRequest] = {}
        self.mr_descriptions: dict[str, str] = {}
        self.mr_comments: dict[str, list[MrComment]] = defaultdict(list)
        self.suggestions: dict[str, list[Suggestion]] = defaultdict(list)
        self.pushes: list[tuple[str, str, str]] = []
        self.comment_events: list[HostCommentEvent] = []
        self.clones: list[str] = []
        self._keys: dict[str, object] = {}
        self._seq = 0

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> FakeGitHost:
        root = ctx.base_dir / str(settings.get("repos_root", "var/sim/repos"))
        auth: dict[str, HostAuth] = {}
        available = []
        for host, cred in ctx.config.git.host_credentials.items():
            try:
                token = ctx.secrets.resolve(cred.token, f"git.host_credentials.{host}.token")
                user = ctx.secrets.resolve(cred.username, "username") if (cred.username.env or cred.username.file) else "oauth2"
            except SecretError:
                continue
            auth[host] = HostAuth(user, token)
            available.append(host)
        identity = str(settings.get("identity", "svc-factory"))
        git = Git(options_from_config(ctx.config, auth, allow_protocols="file", author_name=identity,
                                      author_email=f"{identity}@sim.invalid"), ctx.redactor)
        return cls(root, git, ctx.clock, identity, settings.get("requires_credentials", ()), available)

    # ---- fixture helpers ------------------------------------------------
    def url(self, path: str) -> str:
        return (self.repos_root / f"{path}.git").as_uri()

    def create_repo(self, path: str, files: Mapping[str, str | bytes], branch: str = "main",
                    submodules: Mapping[str, str] | None = None, symlinks: Mapping[str, str] | None = None) -> str:
        """Create a bare repo with one commit. submodules maps a path to the URL
        recorded in .gitmodules (relative, other-host, or anything else)."""
        bare = self.repos_root / f"{path}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        self.git.run(["init", "-q", "--bare", "-b", branch, str(bare)])
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            self.git.run(["clone", "-q", bare.as_uri(), str(work)])
            self.git.run(["checkout", "-q", "-B", branch], cwd=work)
            _write_files(work, files)
            for link, target in (symlinks or {}).items():
                (work / link).parent.mkdir(parents=True, exist_ok=True)
                (work / link).symlink_to(target)
            for sub_path, sub_url in (submodules or {}).items():
                source = self._resolve_fixture_url(sub_url, path)
                self.git.run(["submodule", "add", "-q", source, sub_path], cwd=work)
                self.git.run(["config", "-f", ".gitmodules", f"submodule.{sub_path}.url", sub_url], cwd=work)
            self.git.run(["add", "-A"], cwd=work)
            self.git.run(["commit", "-q", "-m", "initial"], cwd=work)
            self.git.run(["push", "-q", "origin", branch], cwd=work)
            return self.git.head(work)

    def commit(self, path: str, files: Mapping[str, str | bytes], branch: str = "main", message: str = "update",
               submodule_pointer: tuple[str, str] | None = None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            self.git.run(["clone", "-q", "-b", branch, self.url(path), str(work)])
            _write_files(work, files)
            if submodule_pointer:
                sub, sha = submodule_pointer
                self.git.run(["update-index", "--add", "--cacheinfo", f"160000,{sha},{sub}"], cwd=work)
            self.git.run(["add", "-A"], cwd=work)
            self.git.run(["commit", "-q", "--allow-empty", "-m", message], cwd=work)
            self.git.run(["push", "-q", "origin", branch], cwd=work)
            return self.git.head(work)

    def _resolve_fixture_url(self, url: str, parent: str) -> str:
        if url.startswith("../") or url.startswith("./"):
            return (self.repos_root / f"{parent}.git" / url).resolve().as_uri()
        for match, replace in self.git.options.url_rewrites:
            if url.startswith(match):
                return replace + url[len(match):]
        return url

    def add_mr_comment(self, mr_id: str, author: str, body: str, thread_id: str | None = None) -> MrComment:
        self._seq += 1
        comment = MrComment(f"n{self._seq}", thread_id or f"t{self._seq}", Principal(author, author), body,
                            self.clock.now())
        self.mr_comments[mr_id].append(comment)
        return comment

    def branch_files(self, path: str, branch: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "w"
            self.git.run(["clone", "-q", "-b", branch, self.url(path), str(work)])
            return {str(p.relative_to(work)): p.read_text(errors="replace")
                    for p in work.rglob("*") if p.is_file() and ".git" not in p.relative_to(work).parts}

    def branches(self, path: str) -> list[str]:
        out = self.git.out(["for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=self.repos_root / f"{path}.git")
        return out.split()

    # ---- port -----------------------------------------------------------
    def _check_credentials(self, workspace: Path) -> None:
        for path, url, _ in self.git.submodule_entries(workspace):
            host = _host(url)
            if host in self.requires_credentials and host not in self.available_credentials:
                raise SubmoduleFailure(f"submodule {path}: host {host} requires a credential and none is configured", path)

    def clone(self, repo: RepoRef, dest: Path, ref: str | None = None) -> CheckoutResult:
        self.clones.append(repo.path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        opts = self.git.options
        submodules_enabled = opts.submodules_enabled
        opts.submodules_enabled = False
        try:
            self.git.clone(self.url(repo.path), dest, ref or repo.default_branch)
        except GitError as exc:
            raise NotFound(f"repo {repo.path}: {exc}") from None
        finally:
            opts.submodules_enabled = submodules_enabled
        if submodules_enabled:
            self._check_credentials(dest)
        return self.git.update_submodules(dest)

    def pull(self, workspace: Path) -> CheckoutResult:
        return self.git.pull(workspace)

    def create_branch(self, workspace: Path, name: str, from_ref: str) -> None:
        self.git.run(["checkout", "-q", "-B", name, from_ref], cwd=workspace)

    def push_branch(self, workspace: Path, branch: str) -> str:
        proc = self.git.run(["push", "-q", "origin", f"refs/heads/{branch}:refs/heads/{branch}"], cwd=workspace, check=False)
        if proc.returncode != 0:
            raise Conflict(f"push of {branch} rejected (non-fast-forward or protected)")
        sha = self.git.head(workspace)
        remote = self.git.out(["remote", "get-url", "origin"], cwd=workspace)
        self.pushes.append((remote, branch, sha))
        return sha

    def compare_url(self, repo: RepoRef, base: str, head: str) -> str | None:
        return f"fake://{repo.path}/-/compare/{base}...{head}"

    def open_merge_request(self, draft: MergeRequestDraft) -> MergeRequest:
        if draft.idempotency_key in self._keys:
            return self._keys[draft.idempotency_key]
        for mr in self.mrs.values():
            if mr.repo.path == draft.repo.path and mr.source_branch == draft.source_branch and mr.state is MergeRequestState.OPEN:
                return mr
        self._seq += 1
        mr_id = str(len(self.mrs) + 1)
        mr = MergeRequest(draft.repo, mr_id, f"fake://{draft.repo.path}/-/merge_requests/{mr_id}",
                          draft.source_branch, draft.target_branch, MergeRequestState.OPEN)
        self.mrs[mr_id] = mr
        self.mr_descriptions[mr_id] = draft.description
        self._keys[draft.idempotency_key] = mr
        return mr

    def set_mr_state(self, mr_id: str, state: MergeRequestState) -> None:
        self.mrs[mr_id] = dataclasses.replace(self.mrs[mr_id], state=state)

    def get_merge_request(self, repo: RepoRef, mr_id: str) -> MergeRequest:
        try:
            return self.mrs[mr_id]
        except KeyError:
            raise NotFound(f"merge request {mr_id}") from None

    def list_mr_comments(self, repo: RepoRef, mr_id: str, cursor: Cursor | None = None) -> Page[MrComment]:
        items = self.mr_comments[mr_id]
        start = next((i + 1 for i, c in enumerate(items) if c.id == cursor), 0) if cursor else 0
        page = tuple(items[start:])
        return Page(page, page[-1].id if page else cursor)

    def reply_in_thread(self, repo: RepoRef, mr_id: str, thread_id: str, body: str, idempotency_key: str) -> MrComment:
        if idempotency_key in self._keys:
            return self._keys[idempotency_key]
        self._seq += 1
        comment = MrComment(f"n{self._seq}", thread_id, self.identity, body, self.clock.now())
        self.mr_comments[mr_id].append(comment)
        self._keys[idempotency_key] = comment
        return comment

    def post_suggestion(self, repo: RepoRef, mr_id: str, suggestion: Suggestion, idempotency_key: str) -> MrComment:
        if idempotency_key in self._keys:
            return self._keys[idempotency_key]
        self.suggestions[mr_id].append(suggestion)
        body = f"{suggestion.note}\n```suggestion:-0+{suggestion.line_end - suggestion.line_start}\n{suggestion.replacement}\n```"
        return self.reply_in_thread(repo, mr_id, f"s{len(self.suggestions[mr_id])}", body, idempotency_key)

    def poll_comment_events(self, cursor: Cursor | None = None) -> Page[HostCommentEvent]:
        start = int(cursor or 0)
        return Page(tuple(self.comment_events[start:]), str(len(self.comment_events)))


def _write_files(root: Path, files: Mapping[str, str | bytes]) -> None:
    for rel, content in files.items():
        target = root / rel
        if content is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)


def _host(url: str) -> str:
    if "://" in url:
        return url.split("://", 1)[1].split("/", 1)[0].split("@")[-1]
    if "@" in url and ":" in url:
        return url.split("@", 1)[1].split(":", 1)[0]
    return ""

