from datetime import UTC, datetime, timedelta

import pytest

from factory.adapters.fake.accounting import MemoryAccounting
from factory.adapters.fake.clock import FakeClock
from factory.adapters.models.sqlite_accounting import SqliteAccounting
from factory.ports.types import CallContext, SpendRecord, SpendScope, SpendScopeKind, Usage


@pytest.fixture(params=["memory", "sqlite"])
def acct(request, tmp_path):
    clock = FakeClock(datetime(2026, 1, 5, tzinfo=UTC))
    limits = {SpendScopeKind.USER: 1000, SpendScopeKind.TEAM: 0, SpendScopeKind.AGENT: 0}
    a = MemoryAccounting(clock, limits) if request.param == "memory" else SqliteAccounting(tmp_path / "a.db", clock, limits)
    return a, clock


def test_spend_attribution_and_daily_budget(acct):
    a, clock = acct
    ctx = CallContext("bot", "SIM-1", "implement", "alice", "frontend")
    a.record(SpendRecord(clock.now(), ctx, "m", Usage(600, 100)))
    assert a.spent_tokens(SpendScope(SpendScopeKind.TICKET, "SIM-1")) == 700
    assert a.spent_tokens(SpendScope(SpendScopeKind.TEAM, "frontend")) == 700
    decision = a.check(ctx, 400)
    assert not decision.allowed and decision.scope == SpendScope(SpendScopeKind.USER, "alice")
    clock.advance(days=2)
    assert a.check(ctx, 400).allowed
    assert a.spent_tokens(SpendScope(SpendScopeKind.USER, "alice"), clock.now() - timedelta(days=1)) == 0
