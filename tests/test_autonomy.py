"""Autonomy: the agent pushes itself to finish instead of stopping mid-task."""

from __future__ import annotations

from pathlib import Path

import pytest

from standalone import agent as agent_mod
from standalone import memory


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))
    monkeypatch.delenv("ANALYST_PC_TOOLS", raising=False)


class Script:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def complete(self, system, messages, schemas, on_delta=None):
        self.calls.append([dict(message) for message in messages])
        return self.responses.pop(0)


def tool_call(call_id: str = "t1") -> dict:
    return {
        "content": None,
        "tool_calls": [
            {"id": call_id, "name": "save_fact", "arguments": {"key": call_id, "value": "v"}}
        ],
    }


@pytest.fixture()
def conv(tmp_path: Path) -> memory.Conversation:
    return memory.Conversation("auto", tmp_path).load()


def test_agent_pushes_itself_until_done(conv: memory.Conversation):
    client = Script(
        [
            tool_call("t1"),
            {"content": "сделал часть задачи"},
            tool_call("t2"),
            {"content": "ГОТОВО: задание выполнено полностью"},
        ]
    )
    answer = agent_mod.run_turn(conv, client, "сделай задание")
    assert answer == "задание выполнено полностью"
    assert len(client.calls) == 4
    pushed = client.calls[2][-1]
    assert pushed["role"] == "user"
    assert "не complete" in pushed["content"] or "continue" in pushed["content"]
    user_events = [m for m in conv.messages if m.get("role") == "user"]
    assert len(user_events) == 1
    partials = [m for m in conv.messages if m.get("role") == "assistant" and m.get("content") == "сделал часть задачи"]
    assert partials


def test_agent_can_be_disabled_via_env(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_AUTO_CONTINUE", "0")
    client = Script(
        [
            tool_call("t1"),
            {"content": "частичный ответ"},
        ]
    )
    answer = agent_mod.run_turn(conv, client, "сделай задание")
    assert answer == "частичный ответ"
    assert len(client.calls) == 2


def test_no_push_for_plain_chat(conv: memory.Conversation):
    client = Script([{"content": "просто ответ"}])
    answer = agent_mod.run_turn(conv, client, "как дела?")
    assert answer == "просто ответ"
    assert len(client.calls) == 1


def test_push_limit_is_respected(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_AUTO_CONTINUE", "2")
    client = Script(
        [
            tool_call("t1"),
            {"content": "часть 1"},
            {"content": "часть 2"},
            {"content": "часть 3"},
        ]
    )
    answer = agent_mod.run_turn(conv, client, "сделай задание")
    assert answer == "часть 3"
    assert len(client.calls) == 4


def test_done_markers():
    assert agent_mod._is_done_marker("ГОТОВО: всё сделал")
    assert agent_mod._is_done_marker("готово: всё")
    assert not agent_mod._is_done_marker("сделал ГОТОВО наполовину")
    assert agent_mod._strip_done_marker("ГОТОВО: итог") == "итог"
    assert agent_mod._strip_done_marker("готово итог") == "итог"
    assert agent_mod._strip_done_marker("обычный ответ") == "обычный ответ"
    assert agent_mod._strip_done_marker("ГОТОВО") == "ГОТОВО"
