"""Every model call, from any stage or harness, goes through here: per-ticket
cap (per agent), daily user/team/agent budgets, accounting, and trace capture."""

from __future__ import annotations

from factory.ports.errors import BudgetExceeded
from factory.ports.types import (
    CallContext, Message, ModelRequest, ModelResponse, SpendRecord, SpendScope, SpendScopeKind, ToolSpec,
)


class BudgetedModels:
    def __init__(self, providers, accounting, clock, max_tokens_per_ticket: int, recorder=None) -> None:
        self.providers = providers
        self.accounting = accounting
        self.clock = clock
        self.max_tokens_per_ticket = max_tokens_per_ticket
        self.recorder = recorder

    def ticket_spend(self, ticket_key: str) -> int:
        return self.accounting.spent_tokens(SpendScope(SpendScopeKind.TICKET, ticket_key))

    def complete(self, provider: str, model: str, messages: tuple[Message, ...], context: CallContext,
                 tools: tuple[ToolSpec, ...] = (), max_output_tokens: int = 4096) -> ModelResponse:
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
        request = ModelRequest(model, messages, context, tools, max_output_tokens)
        response = self.providers(provider).complete(request)
        self.accounting.record(SpendRecord(self.clock.now(), context, model, response.usage))
        if self.recorder is not None:
            self.recorder.model_call(context.stage, provider, model, request, response)
        return response
