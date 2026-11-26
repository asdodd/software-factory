"""Every model call, from any stage or harness, goes through here.

The deployment is limited by requests per hour, so requests are budgeted as
well as tokens: a per-ticket request cap fails the ticket like the spend cap,
and a global requests-per-hour window makes calls wait for room (up to
max_wait_seconds) instead of overrunning the endpoint, then fail with
RateLimited. Daily user/team/agent token budgets, accounting, and trace
capture apply as before."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from factory.ports.errors import BudgetExceeded, RateLimited
from factory.ports.types import (
    CallContext,
    Message,
    ModelRequest,
    ModelResponse,
    SpendRecord,
    SpendScope,
    SpendScopeKind,
    ToolSpec,
)


class BudgetedModels:
    def __init__(self, providers, accounting, clock, max_tokens_per_ticket: int, recorder=None,
                 max_requests_per_ticket: int = 0, requests_per_hour: int = 0, max_wait_seconds: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.providers = providers
        self.accounting = accounting
        self.clock = clock
        self.max_tokens_per_ticket = max_tokens_per_ticket
        self.recorder = recorder
        self.max_requests_per_ticket = max_requests_per_ticket
        self.requests_per_hour = requests_per_hour
        self.max_wait_seconds = max_wait_seconds
        self.sleep = sleep
        self.monotonic = monotonic

    @classmethod
    def for_config(cls, providers, accounting, clock, cfg, recorder=None) -> BudgetedModels:
        b = cfg.models.budgets
        return cls(providers, accounting, clock, cfg.limits.max_tokens_per_ticket, recorder,
                   cfg.limits.max_requests_per_ticket, b.requests_per_hour, float(b.max_wait_seconds))

    def _wait_for_hourly_room(self) -> None:
        if not self.requests_per_hour:
            return
        started = self.monotonic()
        while self.accounting.request_count(None, self.clock.now() - timedelta(hours=1)) >= self.requests_per_hour:
            waited = self.monotonic() - started
            if waited >= self.max_wait_seconds:
                raise RateLimited(f"the hourly budget of {self.requests_per_hour} model requests is used up")
            self.sleep(min(30.0, self.max_wait_seconds - waited))

    def ticket_spend(self, ticket_key: str) -> int:
        return self.accounting.spent_tokens(SpendScope(SpendScopeKind.TICKET, ticket_key))

    def complete(self, provider: str, model: str, messages: tuple[Message, ...], context: CallContext,
                 tools: tuple[ToolSpec, ...] = (), max_output_tokens: int = 4096) -> ModelResponse:
        if self.max_requests_per_ticket:
            used = self.accounting.request_count(SpendScope(SpendScopeKind.TICKET, context.ticket_key))
            if used >= self.max_requests_per_ticket:
                raise BudgetExceeded(f"ticket {context.ticket_key} reached its cap of "
                                     f"{self.max_requests_per_ticket} model requests")
        spent = self.ticket_spend(context.ticket_key)
        if spent + max_output_tokens > self.max_tokens_per_ticket:
            raise BudgetExceeded(
                f"ticket {context.ticket_key} would exceed its cap of {self.max_tokens_per_ticket} tokens (spent {spent})"
            )
        decision = self.accounting.check(context, max_output_tokens)
        if not decision.allowed:
            raise BudgetExceeded(
                f"{decision.scope.kind.value} {decision.scope.id} is over its daily budget "
                f"({decision.spent_tokens}/{decision.limit_tokens})"
            )
        self._wait_for_hourly_room()
        request = ModelRequest(model, messages, context, tools, max_output_tokens)
        response = self.providers(provider).complete(request)
        self.accounting.record(SpendRecord(self.clock.now(), context, model, response.usage))
        if self.recorder is not None:
            self.recorder.model_call(context.stage, provider, model, request, response)
        return response
