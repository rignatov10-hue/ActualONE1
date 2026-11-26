"""Autonomy: the agent pushes itself to finish instead of stopping mid-task."""

from __future__ import annotations

import json
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


def test_truncated_text_is_continued(conv: memory.Conversation):
    """A reply cut off by the output limit gets continued even without any tool use."""
    client = Script(
        [
            {"content": "первая половина длинного текста, которая", "stop_reason": "length"},
            {"content": " внезапно оборвалась на полуслове", "stop_reason": "length"},
            {"content": "ГОТОВО: текст дописан целиком"},
        ]
    )
    answer = agent_mod.run_turn(conv, client, "напиши длинный текст")
    assert answer == "текст дописан целиком"
    assert len(client.calls) == 3
    # push is session-local: only one real user message in the dialog
    assert len([m for m in conv.messages if m["role"] == "user"]) == 1
    # the push asks to continue exactly where the text stopped
    push = client.calls[1][-1]
    assert push["role"] == "user" and "output length limit" in push["content"]


def test_plain_chat_without_truncation_not_pushed(conv: memory.Conversation):
    client = Script([{"content": "короткий ответ", "stop_reason": "end_turn"}])
    answer = agent_mod.run_turn(conv, client, "привет")
    assert answer == "короткий ответ"
    assert len(client.calls) == 1


def test_max_tokens_payload_default_and_env(monkeypatch: pytest.MonkeyPatch):
    from standalone import llm

    monkeypatch.delenv("ANALYST_MAX_TOKENS", raising=False)
    openai_payload = llm.OpenAICompatClient("m", "http://127.0.0.1:5001/v1")._payload("s", [], [])
    anthropic_payload = llm.AnthropicClient("m", "k")._payload("s", [], [])
    assert openai_payload["max_tokens"] == 8192
    assert anthropic_payload["max_tokens"] == 8192
    monkeypatch.setenv("ANALYST_MAX_TOKENS", "2048")
    assert llm.OpenAICompatClient("m", "http://x/v1")._payload("s", [], [])["max_tokens"] == 2048
    assert llm._max_tokens() == 2048
    monkeypatch.setenv("ANALYST_MAX_TOKENS", "мусор")
    assert llm._max_tokens() == 8192


def test_openai_finish_reason_length_maps_to_length():
    from standalone import llm

    class FakeResp:
        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "кусок"}, "finish_reason": "length"}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import urllib.request

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: FakeResp()
    try:
        client = llm.OpenAICompatClient("m", "http://127.0.0.1:5001/v1")
        result = client.complete("s", [{"role": "user", "content": "x"}], [])
    finally:
        urllib.request.urlopen = orig
    assert result["stop_reason"] == "length"
    assert result["content"] == "кусок"


def test_openai_finish_reason_stop_maps_to_end_turn():
    from standalone import llm

    class FakeResp:
        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "всё"}, "finish_reason": "stop"}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import urllib.request

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: FakeResp()
    try:
        client = llm.OpenAICompatClient("m", "http://127.0.0.1:5001/v1")
        result = client.complete("s", [{"role": "user", "content": "x"}], [])
    finally:
        urllib.request.urlopen = orig
    assert result["stop_reason"] == "end_turn"


def test_continuation_flag_persists_note_for_next_turn(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch):
    """When pushes run out mid-truncation the next turn continues, not repeats."""
    monkeypatch.setenv("ANALYST_AUTO_CONTINUE", "1")
    client = Script(
        [
            {"content": "часть один ", "stop_reason": "length"},
            {"content": "часть два", "stop_reason": "length"},
        ]
    )
    answer = agent_mod.run_turn(conv, client, "напиши текст")
    assert answer == "часть один часть два"
    assert memory.has_continuation_flag(conv)

    client2 = Script([{"content": "и финальный хвост"}])
    client2.systems = []

    original_complete = client2.complete

    def recording_complete(system, messages, schemas, on_delta=None):
        client2.systems.append(system)
        return original_complete(system, messages, schemas, on_delta)

    client2.complete = recording_complete
    answer2 = agent_mod.run_turn(conv, client2, "пиши дальше")
    assert answer2 == "и финальный хвост"
    assert "output length limit" in client2.systems[0]
    assert not memory.has_continuation_flag(conv)


def test_continuation_flag_cleared_even_on_unrelated_turn(conv: memory.Conversation):
    memory.set_continuation_flag(conv)
    client = Script([{"content": "обычный ответ"}])
    answer = agent_mod.run_turn(conv, client, "привет")
    assert answer == "обычный ответ"
    assert not memory.has_continuation_flag(conv)


def test_truncated_final_without_pushes_sets_flag(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_AUTO_CONTINUE", "0")
    client = Script([{"content": "обрыв посреди", "stop_reason": "length"}])
    answer = agent_mod.run_turn(conv, client, "напиши текст")
    assert answer == "обрыв посреди"
    assert memory.has_continuation_flag(conv)


def test_delete_conversation_removes_flag(conv: memory.Conversation):
    memory.set_continuation_flag(conv)
    assert memory.delete_conversation(conv.root, conv.name) is True
    assert not memory.has_continuation_flag(conv)
