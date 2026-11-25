"""Tests of the advanced runtime: tracing, approvals, facts, summarization, web_read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from standalone import agent as agent_mod
from standalone import memory, pc
from standalone.memory import facts_section, load_facts, save_fact

from test_standalone import MockHandler, make_frame, mock_server  # noqa: F401


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))
    monkeypatch.delenv("ANALYST_PC_TOOLS", raising=False)


@pytest.fixture()
def conv(tmp_path: Path) -> memory.Conversation:
    conversation = memory.Conversation("adv", tmp_path).load()
    conversation.set_dataset(
        memory.Dataset(source="local", title="d.csv", load_time="t"),
        make_frame(),
    )
    return conversation


def test_tool_call_is_traced(conv: memory.Conversation):
    agent_mod.execute_tool(conv, {"name": "describe_columns", "arguments": {}})
    trace_file = conv.root / "traces" / f"{conv.name}.jsonl"
    assert trace_file.exists()
    events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "tool"
    assert events[-1]["tool"] == "describe_columns"
    assert "columns" in events[-1]["result"]


def test_unknown_tool_traces_error(conv: memory.Conversation):
    payload = json.loads(agent_mod.execute_tool(conv, {"name": "nope", "arguments": {}}))
    assert "error" in payload
    events = [json.loads(line) for line in (conv.root / "traces" / "adv.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["tool"] == "nope"


def test_approval_hook_gate(conv: memory.Conversation):
    conv.approval_hook = lambda question: "delete" in question
    approved = json.loads(
        agent_mod.execute_tool(conv, {"name": "request_approval", "arguments": {"question": "delete folder X?"}})
    )
    assert approved["approved"] is True
    denied = json.loads(
        agent_mod.execute_tool(conv, {"name": "request_approval", "arguments": {"question": "send email?"}})
    )
    assert denied["approved"] is False
    empty = json.loads(agent_mod.execute_tool(conv, {"name": "request_approval", "arguments": {}}))
    assert "error" in empty


def test_approval_without_channel_refuses(conv: memory.Conversation):
    conv.approval_hook = None
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "request_approval", "arguments": {"question": "delete X?"}})
    )
    assert payload["approved"] is False
    assert "refuse" in payload["note"]


def test_activity_hook_notified_on_approval(conv: memory.Conversation):
    seen: list[str] = []
    conv.approval_hook = lambda question: True
    conv.activity_hook = seen.append
    agent_mod.execute_tool(conv, {"name": "request_approval", "arguments": {"question": "rename file?"}})
    assert any("ПОДТВЕРЖДЕНИЯ" in item for item in seen)


def test_save_fact_tool_and_section(conv: memory.Conversation):
    payload = json.loads(
        agent_mod.execute_tool(
            conv, {"name": "save_fact", "arguments": {"key": "user.team", "value": "оксотдел"}}
        )
    )
    assert payload["total_facts"] == 1
    facts = load_facts(conv.root)
    assert facts["user.team"] == "оксотдел"
    section = facts_section(facts)
    assert "user.team" in section
    deleted = json.loads(
        agent_mod.execute_tool(conv, {"name": "save_fact", "arguments": {"key": "user.team", "value": ""}})
    )
    assert deleted["deleted"] is True
    assert load_facts(conv.root) == {}
    assert facts_section({}) == ""


def test_approval_and_fact_tools_in_schemas():
    _, schemas = agent_mod._system_state()
    names = {schema["name"] for schema in schemas}
    assert {"request_approval", "save_fact"} <= names


def test_facts_reach_system_instructions(mock_server: str, tmp_path: Path):
    from standalone import llm

    client = llm.OpenAICompatClient("m", mock_server, "k")
    conv = memory.Conversation("facts-e2e", tmp_path).load()
    save_fact(tmp_path, "user.name", "Роман")
    MockHandler.script = [{"choices": [{"message": {"content": "ok"}}]}]
    agent_mod.run_turn(conv, client, "привет")
    first = next(r for r in MockHandler.requests if r["path"].endswith("/chat/completions"))
    system_content = first["body"]["messages"][0]["content"]
    assert "Роман" in system_content
    assert "user.name" in system_content


def test_summarization_kicks_in_on_long_history(mock_server: str, tmp_path: Path):
    from standalone import llm

    client = llm.OpenAICompatClient("m", mock_server, "k")
    conv = memory.Conversation("sum", tmp_path).load()
    for index in range(60):
        conv.append({"role": "user", "content": f"msg {index}"})
        conv.append({"role": "assistant", "content": f"answer {index}"})
    MockHandler.script = [
        {"choices": [{"message": {"content": "Резюме: обсуждали 60 сообщений."}}]},
        {"choices": [{"message": {"content": "готово"}}]},
    ]
    answer = agent_mod.run_turn(conv, client, "ещё вопрос")
    assert answer == "готово"
    model_messages = conv.model_messages()
    assert len(conv.messages) <= 14
    assert len(model_messages) <= 13
    assert conv.latest_summary().startswith("Резюме")
    summary_messages = MockHandler.requests[0]["body"]["messages"]
    assert "Compress" in summary_messages[1]["content"]
    assert "msg 54" in summary_messages[2]["content"]
    trace = json.loads((tmp_path / "traces" / "sum.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert trace["event"] == "summarize"


def test_short_history_is_not_summarized(mock_server: str, tmp_path: Path):
    from standalone import llm

    client = llm.OpenAICompatClient("m", mock_server, "k")
    conv = memory.Conversation("short", tmp_path).load()
    conv.append({"role": "user", "content": "q1"})
    MockHandler.script = [{"choices": [{"message": {"content": "ok"}}]}]
    agent_mod.run_turn(conv, client, "привет")
    assert conv.latest_summary() == ""
    request_bodies = [r["body"] for r in MockHandler.requests if r["path"].endswith("/chat/completions")]
    assert all("Compress" not in body["messages"][0]["content"] for body in request_bodies)


def test_web_read_strips_html(conv: memory.Conversation, mock_server: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    MockHandler.extra_files["/page"] = b"<html><head><style>.x{}</style></head><body><h1>Tool</h1><p>Hello world</p></body></html>"
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "web_read", "arguments": {"url": f"{mock_server}/page"}})
    )
    assert "Hello world" in payload["content"]
    assert "<" not in payload["content"]
    bad = json.loads(
        agent_mod.execute_tool(conv, {"name": "web_read", "arguments": {"url": "notaurl"}})
    )
    assert "error" in bad
