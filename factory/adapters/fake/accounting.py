from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from factory.ports.context import AdapterContext
from factory.ports.types import BudgetDecision, CallContext, SpendRecord, SpendScope, SpendScopeKind


def _scope_ids(ctx: CallContext) -> dict[SpendScopeKind, str]:
    return {SpendScopeKind.TICKET: ctx.ticket_key, SpendScopeKind.USER: ctx.attributed_to,
            SpendScopeKind.TEAM: ctx.team, SpendScopeKind.AGENT: ctx.agent}


class MemoryAccounting:
    """Daily budgets per user, team, and agent (0 = unlimited). The per-ticket
    cap is per agent and is enforced by the core budget wrapper."""

    def __init__(self, clock, daily_limits: Mapping[SpendScopeKind, int]) -> None:
        self._clock = clock
        self._limits = dict(daily_limits)
        self.records: list[SpendRecord] = []

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], ctx: AdapterContext) -> MemoryAccounting:
        b = ctx.config.models.budgets
        return cls(ctx.clock, {SpendScopeKind.USER: b.user_tokens_per_day, SpendScopeKind.TEAM: b.team_tokens_per_day,
                               SpendScopeKind.AGENT: b.agent_tokens_per_day})

    def record(self, record: SpendRecord) -> None:
        self.records.append(record)

    def spent_tokens(self, scope: SpendScope, since: datetime | None = None) -> int:
        total = 0
        for r in self.records:
            if since is not None and r.at < since:
                continue
            if _scope_ids(r.context)[scope.kind] == scope.id:
                total += r.usage.total
        return total

    def check(self, context: CallContext, estimated_tokens: int) -> BudgetDecision:
        since = self._clock.now() - timedelta(days=1)
        for kind, limit in self._limits.items():
            if not limit:
                continue
            scope = SpendScope(kind, _scope_ids(context)[kind])
            spent = self.spent_tokens(scope, since)
            if spent + estimated_tokens > limit:
                return BudgetDecision(False, scope, spent, limit)
        return BudgetDecision(True)
