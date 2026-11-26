"""Durable accounting in SQLite (stdlib). Same semantics as the in-memory
accounting: daily user, team, and agent budgets; per-ticket caps are enforced
by the core budget wrapper."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from factory.ports.context import AdapterContext
from factory.ports.types import BudgetDecision, CallContext, SpendRecord, SpendScope, SpendScopeKind

COLUMNS = {SpendScopeKind.TICKET: "ticket", SpendScopeKind.USER: "user", SpendScopeKind.TEAM: "team",
           SpendScopeKind.AGENT: "agent"}


class SqliteAccounting:
    def __init__(self, path: Path, clock, daily_limits: Mapping[SpendScopeKind, int]) -> None:
        self.path = path
        self.clock = clock
        self.limits = dict(daily_limits)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> SqliteAccounting:
        b = ctx.config.models.budgets
        return cls(ctx.base_dir / str(settings.get("path", "var/accounting.db")), ctx.clock,
                   {SpendScopeKind.USER: b.user_tokens_per_day, SpendScopeKind.TEAM: b.team_tokens_per_day,
                    SpendScopeKind.AGENT: b.agent_tokens_per_day})

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._conn.execute("CREATE TABLE IF NOT EXISTS spend (at TEXT, ticket TEXT, user TEXT, team TEXT, "
                               "agent TEXT, stage TEXT, model TEXT, input INTEGER, output INTEGER)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS spend_at ON spend (at)")
        return self._conn

    def record(self, record: SpendRecord) -> None:
        c = record.context
        with self._lock:
            self._db().execute("INSERT INTO spend VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                record.at.isoformat(), c.ticket_key, c.attributed_to, c.team, c.agent, c.stage, record.model,
                record.usage.input_tokens, record.usage.output_tokens))

    def spent_tokens(self, scope: SpendScope, since: datetime | None = None) -> int:
        col = COLUMNS[scope.kind]
        sql = f"SELECT COALESCE(SUM(input + output), 0) FROM spend WHERE {col} = ?"  # noqa: S608 - column from a fixed map
        args: list = [scope.id]
        if since is not None:
            sql += " AND at >= ?"
            args.append(since.isoformat())
        with self._lock:
            return int(self._db().execute(sql, args).fetchone()[0])

    def request_count(self, scope: SpendScope | None = None, since: datetime | None = None) -> int:
        sql, args = "SELECT COUNT(*) FROM spend WHERE 1 = 1", []
        if scope is not None:
            sql += f" AND {COLUMNS[scope.kind]} = ?"  # noqa: S608 - column from a fixed map
            args.append(scope.id)
        if since is not None:
            sql += " AND at >= ?"
            args.append(since.isoformat())
        with self._lock:
            return int(self._db().execute(sql, args).fetchone()[0])

    def check(self, context: CallContext, estimated_tokens: int) -> BudgetDecision:
        since = self.clock.now() - timedelta(days=1)
        ids = {SpendScopeKind.USER: context.attributed_to, SpendScopeKind.TEAM: context.team,
               SpendScopeKind.AGENT: context.agent}
        for kind, limit in self.limits.items():
            if not limit:
                continue
            scope = SpendScope(kind, ids[kind])
            spent = self.spent_tokens(scope, since)
            if spent + estimated_tokens > limit:
                return BudgetDecision(False, scope, spent, limit)
        return BudgetDecision(True)
