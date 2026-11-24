"""Tests of the JARVIS voice-assistant mode (standalone/jarvis.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from standalone import jarvis, memory


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))
    monkeypatch.delenv("ANALYST_PC_TOOLS", raising=False)
    monkeypatch.delenv("ANALYST_PC_CONFIRM", raising=False)


@pytest.fixture()
def conv(tmp_path: Path) -> memory.Conversation:
    return memory.Conversation("jv", tmp_path).load()


class SilentVoice:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.recent: list[str] = []

    def say(self, text: str) -> None:
        self.spoken.append(text)

    def wait(self) -> None:
        pass


class FakeClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, system, messages, tool_schemas):
        self.calls.append(list(messages))
        return self.answer


@pytest.fixture()
def assistant(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch) -> jarvis.Assistant:
    events: list[tuple[str, str]] = []
    assistant = jarvis.Assistant(FakeClient("{}"), conv, emit=lambda who, text: events.append((who, text)))
    assistant.voice = SilentVoice()
    assistant.events = events
    return assistant


def test_parse_actions_extracts_action_lines():
    raw = "Делаю снимок, сэр.\nACTION: {\"type\": \"screenshot\", \"args\": {}}"
    spoken, actions = jarvis.parse_actions(raw)
    assert spoken == "Делаю снимок, сэр."
    assert actions == [{"type": "screenshot", "args": {}}]


def test_parse_actions_whole_json_variant():
    raw = json.dumps({"say": "Открываю, сэр.", "action": {"type": "open_url", "args": {"url": "https://x.io"}}})
    spoken, actions = jarvis.parse_actions(raw)
    assert spoken == "Открываю, сэр."
    assert actions[0]["type"] == "open_url"


def test_parse_actions_tolerates_broken_json():
    spoken, actions = jarvis.parse_actions("Привет\nACTION: {не json}")
    assert spoken == "Привет"
    assert actions == []


def test_split_wake_matches_and_strips():
    woke, command = jarvis.split_wake("джарвис открой хром")
    assert woke and command == "открой хром"
    woke, command = jarvis.split_wake("просто вопрос")
    assert not woke and command == "просто вопрос"


def test_rule_media_and_greetings(assistant: jarvis.Assistant):
    assistant.handle("выключи звук")
    assert "Звук отключён" in assistant.voice.spoken[0]
    assistant.handle("привет")
    assert "Приветствую" in assistant.voice.spoken[-1]


def test_actions_gated_without_pc_tools(assistant: jarvis.Assistant):
    note = assistant.execute({"type": "screenshot", "args": {}})
    assert "выключено" in note
    note = assistant.execute({"type": "shell", "args": {"command": "echo hi"}})
    assert "выключено" in note


def test_unknown_action_and_open_url_opened(assistant: jarvis.Assistant, monkeypatch: pytest.MonkeyPatch):
    opened: list[str] = []
    monkeypatch.setattr(jarvis.webbrowser, "open", opened.append)
    assistant.execute({"type": "open_url", "args": {"url": "https://example.com"}})
    assert opened == ["https://example.com"]
    assert "Неизвестное действие" in assistant.execute({"type": "teleport", "args": {}})


def test_model_answer_runs_action(assistant: jarvis.Assistant, monkeypatch: pytest.MonkeyPatch):
    assistant.client.answer = (
        "Открываю википедию, сэр.\nACTION: {\"type\": \"open_url\", \"args\": {\"url\": \"https://ru.wikipedia.org\"}}"
    )
    opened: list[str] = []
    monkeypatch.setattr(jarvis.webbrowser, "open", opened.append)
    spoken = assistant.handle("объясни подробнее, что такое энтропия")
    assert "сэр" in spoken
    assert opened == ["https://ru.wikipedia.org"]
    assert assistant.history[-1]["role"] == "assistant"


def test_shell_uses_pc_module_and_audits(assistant: jarvis.Assistant, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    note = assistant.execute({"type": "shell", "args": {"command": "echo jarvis-shell"}})
    assert "jarvis-shell" in note
    assert (assistant.conv.root / "pc_log.jsonl").exists()


def test_shutdown_requires_approval(assistant: jarvis.Assistant):
    assert "отменено" in assistant.execute({"type": "shutdown", "args": {}}).lower()


def test_prompt_and_history_cap():
    assert "ACTION" in jarvis.JARVIS_PROMPT
    assert jarvis.HISTORY_LIMIT >= 4
