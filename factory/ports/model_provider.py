from __future__ import annotations

from typing import Protocol, runtime_checkable

from factory.ports.types import ModelRequest, ModelResponse


@runtime_checkable
class ModelProvider(Protocol):
    """One call, one response, with usage. Budgets and rate limits are
    enforced by a core wrapper around this port, not by each adapter."""

    def complete(self, request: ModelRequest) -> ModelResponse: ...
