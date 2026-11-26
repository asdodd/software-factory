import pytest

from factory.adapters.fake.accounting import MemoryAccounting
from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.model_provider import ScriptedModelProvider
from factory.adapters.http import HttpClient, Response
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


def test_per_ticket_request_cap_is_a_runaway_guard():
    models, _, _ = make(max_requests_per_ticket=2)
    models.complete("p", "m", MSG, ctx())
    models.complete("p", "m", MSG, ctx())
    with pytest.raises(BudgetExceeded, match="cap of 2 model requests"):
        models.complete("p", "m", MSG, ctx())
    models.complete("p", "m", MSG, ctx("SIM-2"))


def test_requests_are_paced_not_failed():
    beats = []
    models, clock, accounting = make(min_interval_seconds=40, on_request=lambda: beats.append(clock.now()))
    for _ in range(4):
        models.complete("p", "m", MSG, ctx())
    times = [r.at for r in accounting.records]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
    assert gaps == [40.0, 40.0, 40.0] and models.waited_seconds == 120.0 and beats


def test_hourly_window_waits_for_room_and_never_fails_by_default():
    models, clock, accounting = make(requests_per_hour=2)
    for _ in range(3):
        models.complete("p", "m", MSG, ctx())
    first, third = accounting.records[0].at, accounting.records[2].at
    assert (third - first).total_seconds() >= 3600


def test_a_positive_max_wait_defers_instead_of_waiting_forever():
    models, _, _ = make(min_interval_seconds=3600, max_wait_seconds=60)
    models.complete("p", "m", MSG, ctx())
    with pytest.raises(RateLimited, match="no request slot within 60s"):
        models.complete("p", "m", MSG, ctx())


def test_patient_http_waits_out_backoff():
    responses = [Response(429, {"Retry-After": "40"}, b"")] * 5 + [Response(200, {}, b"{}")]
    slept = []
    client = HttpClient("https://m.invalid", {}, lambda *a: responses.pop(0), sleep=slept.append, patient=True)
    assert client.request("POST", "/chat/completions", json_body={}).status == 200
    assert slept == [40.0] * 5
    impatient = HttpClient("https://m.invalid", {}, lambda *a: Response(429, {}, b""), sleep=lambda s: None)
    with pytest.raises(RateLimited):
        impatient.request("POST", "/x")
    capped = HttpClient("https://m.invalid", {}, lambda *a: Response(503, {"Retry-After": "40"}, b""),
                        sleep=lambda s: None, patient=True, patient_max_wait_seconds=100)
    with pytest.raises(Exception, match="503"):
        capped.request("POST", "/x")
