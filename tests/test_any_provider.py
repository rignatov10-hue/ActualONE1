"""Any provider / any model support: unknown providers, /models listing, model auto-detect."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from standalone import llm


class ModelsHandler(BaseHTTPRequestHandler):
    """GET /models returns a scripted payload; records whether auth headers came."""

    payload: dict = {}
    requests: list[dict] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        ModelsHandler.requests.append(
            {"path": self.path, "headers": dict(self.headers)}
        )
        data = json.dumps(ModelsHandler.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def models_server():
    server = HTTPServer(("127.0.0.1", 0), ModelsHandler)
    ModelsHandler.payload = {"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}]}
    ModelsHandler.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def test_unknown_provider_with_url_and_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = llm.build_client("xai", "grok-3", "https://api.x.ai/v1", "xai-key")
    assert isinstance(client, llm.OpenAICompatClient)
    assert client.base_url == "https://api.x.ai/v1"
    assert client.model == "grok-3"


def test_unknown_provider_auto_detects_model(models_server, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    url = f"http://127.0.0.1:{models_server.server_port}/v1"
    client = llm.build_client("whatever", None, url, "k")
    assert client.model == "model-a"


def test_unknown_provider_requires_url(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(llm.LLMError, match="Unknown provider 'nope'"):
        llm.build_client("nope", None, None, None)


def test_unknown_provider_requires_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="API key"):
        llm.build_client("nope", "m", "https://api.example.com/v1", None)


def test_preset_respects_explicit_base_url(monkeypatch: pytest.MonkeyPatch):
    client = llm.build_client("deepseek", "deepseek-chat", "https://proxy.example.com/v1", "k")
    assert client.base_url == "https://proxy.example.com/v1"


def test_list_models_openai_format(models_server):
    url = f"http://127.0.0.1:{models_server.server_port}/v1"
    ids = llm.list_models(url, "secret")
    assert ids == ["model-a", "model-b"]
    request = ModelsHandler.requests[-1]
    assert request["path"] == "/v1/models"
    assert request["headers"].get("Authorization") == "Bearer secret"


def test_list_models_anthropic_format(models_server):
    url = f"http://127.0.0.1:{models_server.server_port}/v1"
    ids = llm.list_models(url, "sk-ant", "anthropic")
    assert ids == ["model-a", "model-b"]
    request = ModelsHandler.requests[-1]
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert headers.get("x-api-key") == "sk-ant"
    assert headers.get("anthropic-version") == "2023-06-01"


def test_list_models_errors():
    with pytest.raises(llm.LLMError):
        llm.list_models("http://127.0.0.1:1/v1", "k")
    with pytest.raises(llm.LLMError):
        llm.list_models("", "k")


def test_legacy_behaviors_intact(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = llm.build_client("custom", None, None, None)
    assert client.base_url == "http://127.0.0.1:5001/v1"
    assert client.model == "local-model"
    client = llm.build_client("http://10.0.0.5:8080/v1", "qwen", None, None)
    assert (client.model, client.base_url) == ("qwen", "http://10.0.0.5:8080/v1")
    client = llm.build_client("kobold", None, None, None)
    assert (client.model, client.base_url) == ("local-model", "http://127.0.0.1:5001/v1")


def test_resolve_helpers():
    assert llm.resolve_base_url("deepseek", None) == "https://api.deepseek.com/v1"
    assert llm.resolve_base_url("claude", None) == "https://api.anthropic.com/v1"
    assert llm.resolve_base_url("https://api.x.ai/v1", None) == "https://api.x.ai/v1"
    assert llm.resolve_base_url("whatever", " http://x.example/v1 ") == "http://x.example/v1"
    assert llm.resolve_base_url("whatever", None) == ""
    assert llm.resolve_api_key("deepseek", None) == ""
    assert llm.resolve_api_key("whatever", "k1") == "k1"
