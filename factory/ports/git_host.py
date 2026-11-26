from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from factory.ports.types import (
    CheckoutResult,
    Cursor,
    HostCommentEvent,
    MergeRequest,
    MergeRequestDraft,
    MrComment,
    Page,
    RepoRef,
    Suggestion,
)


@runtime_checkable
class GitHost(Protocol):
    """GitLab or equivalent. Clone options (depth, submodules, url rewrites,
    credentials) come from the adapter's settings, not from callers."""

    def clone(self, repo: RepoRef, dest: Path, ref: str | None = None) -> CheckoutResult:
        """Clone and check out ref (branch or commit), then init and verify submodules."""
        ...

    def pull(self, workspace: Path) -> CheckoutResult:
        """Fetch and fast-forward only. Raises DivergedClone instead of merging."""
        ...

    def create_branch(self, workspace: Path, name: str, from_ref: str) -> None: ...

    def push_branch(self, workspace: Path, branch: str) -> str:
        """Push the agent's own branch. Returns pushed commit. Never force-pushes others' branches."""
        ...

    def compare_url(self, repo: RepoRef, base: str, head: str) -> str | None: ...

    def open_merge_request(self, draft: MergeRequestDraft) -> MergeRequest:
        """Idempotent on draft.idempotency_key."""
        ...

    def get_merge_request(self, repo: RepoRef, mr_id: str) -> MergeRequest: ...

    def list_mr_comments(self, repo: RepoRef, mr_id: str, cursor: Cursor | None = None) -> Page[MrComment]: ...

    def reply_in_thread(self, repo: RepoRef, mr_id: str, thread_id: str, body: str, idempotency_key: str) -> MrComment: ...

    def post_suggestion(self, repo: RepoRef, mr_id: str, suggestion: Suggestion, idempotency_key: str) -> MrComment: ...

    def poll_comment_events(self, cursor: Cursor | None = None) -> Page[HostCommentEvent]:
        """Comments across watched projects, for GitLab-comment intake."""
        ...
