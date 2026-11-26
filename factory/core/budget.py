"""Every model call, from any stage or harness, goes through here.

The deployment's endpoint is request-limited and answers over-limit traffic
with a timed back-off rather than an error, so the factory paces instead of
failing: each call waits for its slot (a minimum spacing between requests and
a rolling requests-per-hour window, shared by every agent through
accounting). By default it waits as long as it takes; a positive
max_wait_seconds turns a long wait into RateLimited, which defers the ticket.
A per-ticket request cap still stops a runaway ticket. Daily token budgets,
accounting, and trace capture apply as before. on_request is called before
each request, which lets a long run renew its lease."""

from __future__ import annotations

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
                 min_interval_seconds: float = 0.0, on_request: Callable[[], None] | None = None) -> None:
        self.providers = providers
        self.accounting = accounting
        self.clock = clock
        self.max_tokens_per_ticket = max_tokens_per_ticket
        self.recorder = recorder
        self.max_requests_per_ticket = max_requests_per_ticket
        self.requests_per_hour = requests_per_hour
        self.max_wait_seconds = max_wait_seconds
        self.min_interval_seconds = min_interval_seconds
        self.on_request = on_request
        self.waited_seconds = 0.0

    @classmethod
    def for_config(cls, providers, accounting, clock, cfg, recorder=None, on_request=None) -> BudgetedModels:
        b = cfg.models.budgets
        return cls(providers, accounting, clock, cfg.limits.max_tokens_per_ticket, recorder,
                   cfg.limits.max_requests_per_ticket, b.requests_per_hour, float(b.max_wait_seconds),
                   float(b.min_request_interval_seconds), on_request)

    def _delay_needed(self) -> float:
        now = self.clock.now()
        delay = 0.0
        if self.min_interval_seconds:
            last = self.accounting.last_request_at()
            if last is not None:
                delay = max(delay, self.min_interval_seconds - (now - last).total_seconds())
        if self.requests_per_hour:
            window = now - timedelta(hours=1)
            if self.accounting.request_count(None, window) >= self.requests_per_hour:
                delay = max(delay, 30.0)
        return delay

    def _wait_for_slot(self) -> None:
        started = self.clock.now()
        while (delay := self._delay_needed()) > 0:
            waited = (self.clock.now() - started).total_seconds()
            if self.max_wait_seconds and waited + delay > self.max_wait_seconds:
                raise RateLimited(f"no request slot within {self.max_wait_seconds:.0f}s "
                                  f"(pacing: {self.min_interval_seconds:.0f}s apart, {self.requests_per_hour}/hour)")
            step = min(delay, 30.0)
            self.clock.sleep(step)
            self.waited_seconds += step
            if self.on_request is not None:
                self.on_request()

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
        self._wait_for_slot()
        if self.on_request is not None:
            self.on_request()
        request = ModelRequest(model, messages, context, tools, max_output_tokens)
        response = self.providers(provider).complete(request)
        self.accounting.record(SpendRecord(self.clock.now(), context, model, response.usage))
        if self.recorder is not None:
            self.recorder.model_call(context.stage, provider, model, request, response)
        return response
