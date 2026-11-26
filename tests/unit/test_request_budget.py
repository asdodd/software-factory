import pytest

from factory.adapters.fake.accounting import MemoryAccounting
from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.model_provider import ScriptedModelProvider
from factory.core.budget import BudgetedModels
from factory.ports.errors import BudgetExceeded, RateLimited
from factory.ports.types import CallContext, Message, Role

MSG = (Message(Role.USER, "x"),)


def make(**kw):
    provider = ScriptedModelProvider()
    provider.queue("implement", "ok", repeat=True)
    clock = FakeClock()
    accounting = MemoryAccounting(clock, {})
    return BudgetedModels(lambda name: provider, accounting, clock, 10**9, **kw), clock, accounting


def ctx(ticket="SIM-1"):
    return CallContext("bot", ticket, "implement", "alice", "team")


def test_per_ticket_request_cap():
    models, _, _ = make(max_requests_per_ticket=2)
    models.complete("p", "m", MSG, ctx())
    models.complete("p", "m", MSG, ctx())
    with pytest.raises(BudgetExceeded, match="cap of 2 model requests"):
        models.complete("p", "m", MSG, ctx())
    models.complete("p", "m", MSG, ctx("SIM-2"))


def test_hourly_window_waits_for_room_then_fails():
    slept = []
    clock_box = {}

    def sleep(seconds):
        slept.append(seconds)
        clock_box["clock"].advance(seconds=seconds)
        clock_box["t"] += seconds

    clock_box["t"] = 0.0
    models, clock, _ = make(requests_per_hour=2, max_wait_seconds=3600, sleep=sleep, monotonic=lambda: clock_box["t"])
    clock_box["clock"] = clock
    models.complete("p", "m", MSG, ctx())
    clock.advance(minutes=30)
    models.complete("p", "m", MSG, ctx())
    models.complete("p", "m", MSG, ctx())
    assert sum(slept) >= 30 * 60, "the third call waited until the first left the hour window"
    impatient, _, _ = make(requests_per_hour=1, max_wait_seconds=0)
    impatient.complete("p", "m", MSG, ctx())
    with pytest.raises(RateLimited, match="hourly budget of 1 model requests"):
        impatient.complete("p", "m", MSG, ctx())
