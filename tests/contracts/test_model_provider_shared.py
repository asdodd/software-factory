"""Shared ModelProvider contract against the scripted provider, the
OpenAI-compatible adapter (on an emulator), and record-and-replay."""

import pytest

from factory.adapters.fake.model_provider import ScriptedModelProvider
from factory.adapters.http import HttpClient
from factory.adapters.models.openai_compatible import OpenAICompatibleProvider
from factory.adapters.models.replay import ReplayModelProvider
from factory.ports.errors import InvalidRequest, RateLimited
from factory.ports.types import CallContext, Message, ModelRequest, Role, ToolCall, ToolSpec
from tests.fixtures.openai_emulator import OpenAIEmulator

CTX = CallContext("bot", "SIM-1", "implement", "alice", "team")
TOOLS = (ToolSpec("read_file", "read", {"type": "object"}),)


def req(*messages):
    return ModelRequest("m", tuple(messages), CTX, TOOLS)


def make(kind, tmp_path):
    if kind == "scripted":
        p = ScriptedModelProvider()
        p.queue("implement", "", tool_calls=[{"id": "call_0", "name": "read_file", "arguments": {"path": "a"}}],
                input_tokens=11, output_tokens=7)
        p.queue("implement", "done", input_tokens=11, output_tokens=7)
        return p
    em = OpenAIEmulator()
    em.reply(tool_calls=[("read_file", {"path": "a"})])
    em.reply("done")
    openai = OpenAICompatibleProvider(HttpClient(em.base_url, {"Authorization": "Bearer k"}, em, sleep=lambda s: None))
    if kind == "openai":
        return openai
    recorder = ReplayModelProvider(tmp_path / "rec.jsonl", "record", openai)
    first, second = req(Message(Role.USER, "go")), req(Message(Role.USER, "go"), Message(Role.TOOL, "x", (), "call_0"))
    recorder.complete(first)
    recorder.complete(second)
    return ReplayModelProvider(tmp_path / "rec.jsonl", "replay")


@pytest.mark.parametrize("kind", ["scripted", "openai", "replay"])
def test_tool_call_then_answer_with_usage(kind, tmp_path):
    p = make(kind, tmp_path)
    r1 = p.complete(req(Message(Role.USER, "go")))
    assert [(c.name, dict(c.arguments)) for c in r1.message.tool_calls] == [("read_file", {"path": "a"})]
    assert (r1.usage.input_tokens, r1.usage.output_tokens) == (11, 7)
    r2 = p.complete(req(Message(Role.USER, "go"), Message(Role.TOOL, "x", (), "call_0")))
    assert r2.message.content == "done" and r2.message.tool_calls == ()


def test_openai_wire_format_and_retries():
    em = OpenAIEmulator()
    em.reply("ok")
    em.fail_with = [429, 503]
    p = OpenAICompatibleProvider(HttpClient(em.base_url, {"Authorization": "Bearer sk-sim"}, em, sleep=lambda s: None))
    p.complete(req(Message(Role.SYSTEM, "sys"),
                   Message(Role.ASSISTANT, "", (ToolCall("c1", "read_file", {"path": "a"}),)),
                   Message(Role.TOOL, "content", (), "c1")))
    body = em.bodies[-1]
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
    assert body["messages"][2] == {"role": "tool", "content": "content", "tool_call_id": "c1"}
    assert len(em.bodies) == 3 and set(em.auth) == {"Bearer sk-sim"}
    em.fail_with = [429] * 10
    with pytest.raises(RateLimited):
        p.complete(req(Message(Role.USER, "x")))


def test_replay_refuses_unrecorded_requests(tmp_path):
    with pytest.raises(InvalidRequest, match="no recorded response"):
        ReplayModelProvider(tmp_path / "none.jsonl").complete(req(Message(Role.USER, "new")))
