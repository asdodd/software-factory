import json
import urllib.error
import urllib.request

import pytest

from factory.adapters.fake.accounting import MemoryAccounting
from factory.adapters.fake.clock import FakeClock
from factory.adapters.fake.model_provider import ScriptedModelProvider
from factory.core.budget import BudgetedModels
from factory.core.gateway import Gateway
from factory.ports.types import CallContext, Role

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def setup():
    provider = ScriptedModelProvider()
    clock = FakeClock()
    accounting = MemoryAccounting(clock, {})
    models = BudgetedModels(lambda name: provider, accounting, clock, 100_000)
    ctx = CallContext("bot", "SIM-1", "implement", "alice", "team")
    with Gateway(models, "p", "configured-model", ctx) as gw:
        yield gw, provider, accounting


def post(gw, path, body, headers):
    req = urllib.request.Request(gw.base_url + path, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    with OPENER.open(req) as resp:
        return resp.read().decode()


def test_openai_dialect_with_tools(setup):
    gw, provider, accounting = setup
    provider.queue("implement", "", tool_calls=[{"id": "c1", "name": "read_file", "arguments": {"path": "a"}}])
    body = {"model": "anything", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]}
    doc = json.loads(post(gw, "/v1/chat/completions", body, {"Authorization": f"Bearer {gw.token}"}))
    call = doc["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "read_file" and json.loads(call["function"]["arguments"]) == {"path": "a"}
    request = provider.requests[0]
    assert request.model == "configured-model" and request.tools[0].name == "read_file"
    assert [m.role for m in request.messages] == [Role.SYSTEM, Role.USER]
    assert len(accounting.records) == 1


def test_anthropic_dialect_streaming_and_tool_results(setup):
    gw, provider, _ = setup
    provider.queue("implement", "hello there")
    body = {"model": "x", "max_tokens": 50, "stream": True, "system": "sys",
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "ls", "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py"}]}]}
    raw = post(gw, "/v1/messages", body, {"x-api-key": gw.token})
    assert "event: message_start" in raw and "hello there" in raw and "event: message_stop" in raw
    roles = [m.role for m in provider.requests[0].messages]
    assert roles == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.TOOL]


def test_rejects_bad_token(setup):
    gw, _, _ = setup
    with pytest.raises(urllib.error.HTTPError) as err:
        post(gw, "/v1/chat/completions", {"messages": []}, {"Authorization": "Bearer wrong"})
    assert err.value.code == 401


def test_budget_exceeded_is_429_and_recorded():
    provider = ScriptedModelProvider()
    clock = FakeClock()
    models = BudgetedModels(lambda name: provider, MemoryAccounting(clock, {}), clock, 100)
    with Gateway(models, "p", "m", CallContext("bot", "SIM-1", "implement", "a", "t")) as gw:
        with pytest.raises(urllib.error.HTTPError) as err:
            post(gw, "/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]},
                 {"Authorization": f"Bearer {gw.token}"})
        assert err.value.code == 429 and "cap" in gw.budget_exceeded
