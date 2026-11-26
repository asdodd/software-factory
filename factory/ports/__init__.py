"""Ports: the only interfaces core depends on. Core never imports adapters."""

from factory.ports.access_guard import AccessGuard
from factory.ports.accounting import Accounting
from factory.ports.clock import Clock, IdSource
from factory.ports.git_host import GitHost
from factory.ports.harness import AgentHarness
from factory.ports.mailbox import Mailbox
from factory.ports.model_provider import ModelProvider
from factory.ports.monitor import Monitor
from factory.ports.notifier import Notifier
from factory.ports.sandbox import Sandbox
from factory.ports.state_store import StateStore
from factory.ports.ticket_system import TicketSystem
from factory.ports.trace_store import TraceReader, TraceStore, TraceWriter

PORTS: dict[str, type] = {
    "ticket_system": TicketSystem,
    "git_host": GitHost,
    "mailbox": Mailbox,
    "model_provider": ModelProvider,
    "agent_harness": AgentHarness,
    "sandbox": Sandbox,
    "access_guard": AccessGuard,
    "notifier": Notifier,
    "monitor": Monitor,
    "trace_store": TraceStore,
    "accounting": Accounting,
    "state_store": StateStore,
    "clock": Clock,
    "id_source": IdSource,
}

__all__ = [
    "PORTS",
    "AgentHarness",
    "AccessGuard",
    "Accounting",
    "Clock",
    "GitHost",
    "IdSource",
    "Mailbox",
    "ModelProvider",
    "Monitor",
    "Notifier",
    "Sandbox",
    "StateStore",
    "TicketSystem",
    "TraceReader",
    "TraceStore",
    "TraceWriter",
]
