from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from factory.ports.types import BudgetDecision, CallContext, SpendRecord, SpendScope


@runtime_checkable
class Accounting(Protocol):
    def record(self, record: SpendRecord) -> None: ...

    def spent_tokens(self, scope: SpendScope, since: datetime | None = None) -> int: ...

    def request_count(self, scope: SpendScope | None = None, since: datetime | None = None) -> int:
        """Model requests recorded in scope (None: all scopes) since a time."""
        ...

    def check(self, context: CallContext, estimated_tokens: int) -> BudgetDecision:
        """Checks ticket, user, team, and agent budgets; first exhausted scope wins."""
        ...
