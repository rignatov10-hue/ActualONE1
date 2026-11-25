"""Tests of streaming, turn cancellation, the knowledge base and the shell guard."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from standalone import agent as agent_mod
from standalone import knowledge as knowledge_mod
from standalone import llm, memory, pc


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))
    monkeypatch.delenv("ANALYST_PC_TOOLS", raising=False)
    monkeypatch.delenv("ANALYST_KNOWLEDGE_DIR", raising=False)


@pytest.fixture()
def conv(tmp_path) -> memory.Conversation:
    return memory.Conversation("stream", tmp_path).load()


class SseHandler(BaseHTTPRequestHandler):
    script = []
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        SseHandler.requests.append({"path": self.path})
        events = SseHandler.script.pop(0) if SseHandler.script else []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(f"data: {event}\n\n".encode())

    @property
    def port(self):  # pragma: no cover - convenience
        return self.server.server_address[1]


@pytest.fixture()
def sse_server():
    server = HTTPServer(("127.0.0.1", 0), SseHandler)
    SseHandler.script = []
    SseHandler.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_openai_stream_content_and_tools(sse_server):
    SseHandler.script = [
        [
            json.dumps({"choices": [{"delta": {"content": "При"}}]}),
            json.dumps({"choices": [{"delta": {"content": "вет"}}]}),
            "[DONE]",
        ]
    ]
    client = llm.OpenAICompatClient("m", f"http://127.0.0.1:{sse_server.server_address[1]}/v1", "k")
    deltas = []
    result = client.complete("s", [{"role": "user", "content": "q"}], [], on_delta=deltas.append)
    assert result["content"] == "Привет"
    assert "".join(deltas) == "Привет"


def test_openai_stream_tool_call_fragments(sse_server):
    SseHandler.script = [
        [
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "id": "call_1", "function": {"name": "group", "arguments": "{\"b"}}
                                ]
                            }
                        }
                    ]
                }
            ),
            json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\": \"x\"}"}}]}}]}),
            "[DONE]",
        ]
    ]
    client = llm.OpenAICompatClient("m", f"http://127.0.0.1:{sse_server.server_address[1]}/v1", "k")
    result = client.complete("s", [{"role": "user", "content": "q"}], [], on_delta=lambda d: None)
    tool_calls = result["tool_calls"]
    assert tool_calls[0]["name"] == "group"
    assert tool_calls[0]["arguments"] == {"b": "x"}


def test_anthropic_stream_text(sse_server):
    SseHandler.script = [
        [
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Здрав"}}),
            json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ствуй"}}),
            json.dumps({"type": "message_stop"}),
        ]
    ]
    client = llm.AnthropicClient("claude-x", "key", f"http://127.0.0.1:{sse_server.server_address[1]}/v1")
    deltas = []
    result = client.complete("s", [{"role": "user", "content": "q"}], [], on_delta=deltas.append)
    assert result["content"] == "Здравствуй"
    assert deltas == ["Здрав", "ствуй"]


def test_stream_cancel_propagates(sse_server):
    SseHandler.script = [
        [
            json.dumps({"choices": [{"delta": {"content": "часть"}}]}),
            "[DONE]",
        ]
    ]
    client = llm.OpenAICompatClient("m", f"http://127.0.0.1:{sse_server.server_address[1]}/v1", "k")

    def stop(_delta):
        raise llm.StreamCancelled()

    with pytest.raises(llm.StreamCancelled):
        client.complete("s", [{"role": "user", "content": "q"}], [], on_delta=stop)


def test_run_turn_cancel_via_on_delta(conv):
    class Stub:
        def complete(self, system, messages, schemas, on_delta=None):
            if on_delta:
                on_delta("печат")
                on_delta("аю…")
            return {"content": "готово", "tool_calls": []}

    before = agent_mod.CANCEL_NOTE not in [m.get("content", "") for m in conv.messages]
    assert before
    answer = agent_mod.run_turn(conv, Stub(), "привет", on_delta=lambda d: agent_mod.request_cancel())
    assert answer == agent_mod.CANCEL_NOTE
    assert any(m.get("content") == agent_mod.CANCEL_NOTE for m in conv.messages)


def test_run_turn_non_streaming_untouched(conv):
    class Stub:
        def complete(self, system, messages, schemas, on_delta=None):
            assert on_delta is None
            return {"content": "Ответ", "tool_calls": []}

    assert agent_mod.run_turn(conv, Stub(), "вопрос") == "Ответ"


def test_knowledge_search_and_audit(conv, tmp_path, monkeypatch):
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("ANALYST_KNOWLEDGE_DIR", str(kb))
    (kb / "notes.md").write_text("# Проект\nКомпания QW sells rockets to MARS regularly.\n", encoding="utf-8")
    (kb / "empty.txt").write_text("nothing relevant at all\n", encoding="utf-8")
    result = knowledge_mod.search_knowledge(conv, "rockets MARS")
    assert result["matches"] >= 1
    assert result["results"][0]["file"].endswith("notes.md")
    audit = (conv.root / "pc_log.jsonl").read_text(encoding="utf-8")
    assert "search_knowledge" in audit


def test_knowledge_empty_dir_error(conv, tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_KNOWLEDGE_DIR", str(tmp_path / "missing-kb"))
    with pytest.raises(pc.PCError):
        knowledge_mod.search_knowledge(conv, "что-то там важное")


def test_knowledge_tool_via_execute(conv, tmp_path, monkeypatch):
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("ANALYST_KNOWLEDGE_DIR", str(kb))
    (kb / "a.md").write_text("сменить пароль root на сервере alpha:-myq1\n", encoding="utf-8")
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "search_knowledge", "arguments": {"query": "пароль сервере alpha"}})
    )
    assert "error" not in payload and payload["matches"] >= 1


def test_shell_guard_blocks_destructive():
    for bad in ("rm -rf /", "rm -rf ~/", "format C:", "shutdown /s", "shutdown now", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda"):
        with pytest.raises(pc.PCError):
            pc.guard_command(bad)


def test_shell_guard_allows_normal():
    for fine in ("ls -la", "python script.py", "rm -f build.log", "git status", "rmdir temp"):
        pc.guard_command(fine)


def test_guard_wired_into_run_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    conversation = memory.Conversation("guard", tmp_path).load()
    payload = json.loads(
        agent_mod.execute_tool(conversation, {"name": "run_shell", "arguments": {"command": "format C:"}})
    )
    assert "error" in payload and "safety filter" in payload["error"]
