"""End-to-end checks that every provider preset builds and speaks its wire format."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from standalone import agent, llm


class JsonHandler(BaseHTTPRequestHandler):
    """One scripted JSON response per POST, keyed in order; records requests."""

    script: list[dict] = []
    requests: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        JsonHandler.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": json.loads(body) if body else {}}
        )
        payload = JsonHandler.script.pop(0) if JsonHandler.script else {"choices": [{"message": {"content": "ok"}}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def api_server():
    server = HTTPServer(("127.0.0.1", 0), JsonHandler)
    JsonHandler.script = []
    JsonHandler.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


PRESETS = ["kobold", "ollama", "lmstudio", "vllm", "openai", "openrouter", "deepseek", "groq", "mistral-api", "together"]


@pytest.mark.parametrize("provider", PRESETS)
def test_every_openai_preset_builds(provider, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    client = agent.make_client(provider, "test-model", None, "test-key")
    assert isinstance(client, llm.OpenAICompatClient)
    expected = llm.OPENAI_COMPATIBLE_PRESETS[provider][0]
    assert client.base_url == expected


def test_claude_alias_and_base_url_override():
    client = agent.make_client("claude", None, None, "test-key")
    assert isinstance(client, llm.AnthropicClient)
    assert client.base_url == "https://api.anthropic.com/v1"
    custom = agent.make_client("anthropic", None, "http://127.0.0.1:9/v1", "test-key")
    assert custom.base_url == "http://127.0.0.1:9/v1"


def test_deepseek_default_model_without_typing(monkeypatch):
    client = agent.make_client("deepseek", None, None, "test-key")
    assert client.model == "deepseek-chat"
    assert client.base_url == "https://api.deepseek.com/v1"


def test_deepseek_end_to_end_loop(api_server, tmp_path):
    base = f"http://127.0.0.1:{api_server.server_address[1]}/v1"
    # server pretends to be deepseek: chat/completions returns choices
    JsonHandler.script = [
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "describe_columns", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "ГОТОВО: Столбцы такие"}}]},
    ]
    client = llm.OpenAICompatClient("deepseek-chat", base, "test-key")
    from standalone import memory

    conv = memory.Conversation("prov", tmp_path).load()
    answer = agent.run_turn(conv, client, "что в данных?")
    assert answer == "Столбцы такие"
    calls = [r for r in JsonHandler.requests if r["path"].endswith("/chat/completions")]
    assert len(calls) == 2
    second_messages = calls[1]["body"]["messages"]
    assert any(m.get("role") == "tool" for m in second_messages)


def test_claude_end_to_end_loop(api_server, tmp_path):
    base = f"http://127.0.0.1:{api_server.server_address[1]}/v1"
    JsonHandler.script = [
        {
            "content": [
                {"type": "text", "text": "Смотрю данные"},
                {"type": "tool_use", "id": "tu1", "name": "describe_columns", "input": {}},
            ],
            "stop_reason": "tool_use",
        },
        {"content": [{"type": "text", "text": "ГОТОВО: Вот ответ"}], "stop_reason": "end_turn"},
    ]
    client = llm.AnthropicClient("claude-sonnet-4-5", "test-key", base)
    from standalone import memory

    conv = memory.Conversation("prov2", tmp_path).load()
    answer = agent.run_turn(conv, client, "что в данных?")
    assert answer == "Вот ответ"
    calls = [r for r in JsonHandler.requests if r["path"].endswith("/messages")]
    assert len(calls) == 2
    first, second = calls[0]["body"], calls[1]["body"]
    assert first["tools"][0]["input_schema"]["type"] == "object"
    # tool_result appended into the same user message block list
    blocks = second["messages"][-1]["content"]
    assert any(b.get("type") == "tool_result" and b["tool_use_id"] == "tu1" for b in blocks)


def test_local_model_without_any_key(api_server):
    base = f"http://127.0.0.1:{api_server.server_address[1]}/v1"
    client = agent.make_client(base, "qwen-local", None, "")
    assert client.api_key == "local"
