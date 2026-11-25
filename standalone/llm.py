"""Provider-agnostic LLM clients over plain urllib (stdlib only for HTTP).

Any OpenAI-compatible endpoint works: KoboldCpp (http://127.0.0.1:5001/v1),
Ollama, LM Studio, OpenAI, OpenRouter, DeepSeek, Groq, vLLM, and also Mistral's
own API (https://api.mistral.ai/v1) if you want it. Anthropic gets its own
Messages-API client. Both speak one normalized message format.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

_TIMEOUT = 300
_RETRIES = 3


class StreamCancelled(Exception):
    """The consumer of a token stream requested the turn to stop."""

USER_AGENT = "drive-analyst-standalone/1.0"


class LLMError(Exception):
    """A provider request failed after retries."""


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = _TIMEOUT) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers}
    )
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(500).decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < _RETRIES:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                time.sleep(min(float(retry_after) if retry_after else 2 ** attempt, 30))
                last = LLMError(f"{exc.code}: {body[:300]}")
                continue
            raise LLMError(f"The model server answered {exc.code}: {body[:500]}") from exc
        except urllib.error.URLError as exc:
            if attempt + 1 < _RETRIES:
                time.sleep(2**attempt)
                last = LLMError(str(exc))
                continue
            raise LLMError(f"Could not reach the model at {url}: {exc.reason}.") from exc
        except TimeoutError:
            if attempt + 1 < _RETRIES:
                continue
            raise LLMError(f"The model at {url} did not answer in {timeout}s.") from None
    raise LLMError(str(last or "unknown failure"))


def _sse_lines(response: Any) -> Any:
    """Yield parsed JSON payload strings of an SSE response ('data: ...' lines)."""
    for raw in response:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _stream_request(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> Any:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers}
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(500).decode("utf-8", "replace")
        except Exception:
            pass
        raise LLMError(f"The model server answered {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"Could not reach the model at {url}: {exc.reason}.") from exc


class OpenAICompatClient:
    """Talks the /chat/completions wire format: KoboldCpp, Ollama, OpenAI, OpenRouter, ..."""

    def __init__(self, model: str, base_url: str, api_key: str | None = None) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "local"

    def _payload(self, system: str, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
        payload_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for event in messages:
            role = event.get("role")
            if role == "user":
                payload_messages.append({"role": "user", "content": str(event.get("content", ""))})
            elif role == "assistant":
                entry: dict[str, Any] = {"role": "assistant"}
                if event.get("content"):
                    entry["content"] = event["content"]
                if event.get("tool_calls"):
                    entry["tool_calls"] = [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                            },
                        }
                        for call in event["tool_calls"]
                    ]
                if "content" not in entry and "tool_calls" not in entry:
                    entry["content"] = ""
                payload_messages.append(entry)
            elif role == "tool":
                payload_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": event["tool_call_id"],
                        "content": str(event.get("content", "")),
                    }
                )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
        }
        if tool_schemas:
            payload["tools"] = [
                {"type": "function", "function": schema} for schema in tool_schemas
            ]
            payload["tool_choice"] = "auto"
        return payload

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        on_delta: Any = None,
    ) -> dict[str, Any]:
        payload = self._payload(system, messages, tool_schemas)
        if on_delta is None:
            response = _post_json(f"{self.base_url}/chat/completions", self._headers(), payload)
        else:
            response = self._stream(payload, on_delta)
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected completion response: {json.dumps(response)[:300]}") from exc
        tool_calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                {
                    "id": call.get("id") or f"call_{index}",
                    "name": function.get("name", ""),
                    "arguments": arguments,
                }
            )
        return {"content": message.get("content"), "tool_calls": tool_calls}

    def _stream(self, payload: dict[str, Any], on_delta: Any) -> dict[str, Any]:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        content_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        try:
            with _stream_request(f"{self.base_url}/chat/completions", self._headers(), stream_payload, _TIMEOUT) as response:
                for event in _sse_lines(response):
                    choices = event.get("choices") or [{}]
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        content_parts.append(piece)
                        on_delta(piece)
                    for fragment in delta.get("tool_calls") or []:
                        index = fragment.get("index", 0)
                        slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if fragment.get("id"):
                            slot["id"] = fragment["id"]
                        function = fragment.get("function") or {}
                        if function.get("name"):
                            slot["name"] += function["name"] if not slot["name"] else ""
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]
        except StreamCancelled:
            raise
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"Stream failed: {exc}") from exc
        tool_calls = []
        for index in sorted(calls):
            slot = calls[index]
            try:
                arguments = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append({"id": slot["id"] or f"call_{index}", "name": slot["name"], "arguments": arguments})
        return {"choices": [{"message": {"content": "".join(content_parts) or None, "tool_calls": [
            {"id": c["id"], "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
            for c in tool_calls
        ]}}]}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


class AnthropicClient:
    """Talks the Messages API wire format (api.anthropic.com)."""

    BASE_URL = "https://api.anthropic.com/v1"
    VERSION = "2023-06-01"

    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or self.BASE_URL).rstrip("/")

    def _build_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        payload_messages: list[dict[str, Any]] = []
        seen_tool_results: set[str] = set()
        for event in messages:
            role = event.get("role")
            if role == "user":
                payload_messages.append({"role": "user", "content": [{"type": "text", "text": str(event.get("content", ""))}]})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if event.get("content"):
                    blocks.append({"type": "text", "text": event["content"]})
                for call in event.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]})
                if blocks:
                    payload_messages.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                tool_id = event["tool_call_id"]
                if tool_id in seen_tool_results:
                    continue
                seen_tool_results.add(tool_id)
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": str(event.get("content", "")),
                }
                last = payload_messages[-1] if payload_messages else None
                if last and last.get("role") == "user" and isinstance(last.get("content"), list) and any(
                    block.get("type") == "tool_result" for block in last["content"]
                ):
                    last["content"].append(result_block)
                else:
                    payload_messages.append({"role": "user", "content": [result_block]})
        return payload_messages

    def _payload(self, system: str, messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": self._build_messages(messages),
        }
        if tool_schemas:
            payload["tools"] = [
                {"name": schema["name"], "description": schema["description"], "input_schema": schema["parameters"]}
                for schema in tool_schemas
            ]
        return payload

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        on_delta: Any = None,
    ) -> dict[str, Any]:
        payload = self._payload(system, messages, tool_schemas)
        headers = {"x-api-key": self.api_key, "anthropic-version": self.VERSION}
        if on_delta is None:
            response = _post_json(f"{self.base_url}/messages", headers, payload)
        else:
            response = self._stream(payload, headers, on_delta)
        text: str | None = None
        tool_calls = []
        for block in response.get("content") or []:
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                text = (text or "") + block["text"]
            elif kind == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or f"call_{len(tool_calls)}",
                        "name": block.get("name", ""),
                        "arguments": block.get("input") or {},
                    }
                )
        if response.get("stop_reason") == "max_tokens" and not tool_calls and not text:
            raise LLMError("The model hit its output limit before answering.")
        return {"content": text, "tool_calls": tool_calls}

    def _stream(self, payload: dict[str, Any], headers: dict[str, str], on_delta: Any) -> dict[str, Any]:
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}

        def slot_for(index: int) -> dict[str, Any]:
            return calls.setdefault(index, {"id": "", "name": "", "arguments": ""})

        try:
            with _stream_request(f"{self.base_url}/messages", headers, stream_payload, _TIMEOUT) as response:
                for event in _sse_lines(response):
                    kind = event.get("type")
                    if kind == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            slot = slot_for(int(event.get("index", 0)))
                            slot["id"] = block.get("id") or slot["id"]
                            slot["name"] = block.get("name") or slot["name"]
                    elif kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        index = int(event.get("index", 0))
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            text_parts.append(delta["text"])
                            on_delta(delta["text"])
                        elif delta.get("type") == "input_json_delta":
                            slot_for(index)["arguments"] += delta.get("partial_json") or ""
        except (StreamCancelled, LLMError):
            raise
        except Exception as exc:
            raise LLMError(f"Stream failed: {exc}") from exc
        content: list[dict[str, Any]] = []
        joined = "".join(text_parts)
        if joined:
            content.append({"type": "text", "text": joined})
        for index in sorted(calls):
            slot = calls[index]
            try:
                arguments = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
            except json.JSONDecodeError:
                arguments = {}
            content.append(
                {"type": "tool_use", "id": slot["id"] or f"call_{index}", "name": slot["name"], "input": arguments}
            )
        return {"content": content, "stop_reason": "end_turn"}

# Provider presets: name -> (base_url, env var of the key)
OPENAI_COMPATIBLE_PRESETS: dict[str, tuple[str, str]] = {
    "kobold": ("http://127.0.0.1:5001/v1", "OPENAI_API_KEY"),
    "ollama": ("http://127.0.0.1:11434/v1", "OPENAI_API_KEY"),
    "lmstudio": ("http://127.0.0.1:1234/v1", "OPENAI_API_KEY"),
    "vllm": ("http://127.0.0.1:8000/v1", "OPENAI_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENAI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "mistral-api": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
}


def build_client(provider: str, model: str | None, base_url: str | None, api_key: str | None) -> Any:
    import os

    provider = (provider or "").strip().lower().replace("_", "-")
    if provider.startswith("http"):
        provider, base_url = "custom", provider
    if provider in {"anthropic", "claude"}:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMError("Set ANTHROPIC_API_KEY (or pass --api-key).")
        return AnthropicClient(model or "claude-sonnet-4-5", key, base_url)
    if provider in OPENAI_COMPATIBLE_PRESETS:
        base_url, key_env = OPENAI_COMPATIBLE_PRESETS[provider]
        key = api_key or os.environ.get(key_env) or ("local" if base_url.startswith("http://127") or base_url.startswith("http://localhost") else "")
        if not key:
            raise LLMError(f"Set {key_env} (or pass --api-key) for provider '{provider}'.")
        models = {
            "kobold": "local-model",
            "deepseek": "deepseek-chat",
            "openai": "gpt-4o-mini",
            "openrouter": "openai/gpt-4o-mini",
            "groq": "llama-3.3-70b-versatile",
            "mistral-api": "mistral-large-latest",
            "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        }
        return OpenAICompatClient(model or models.get(provider, provider), base_url, key)
    if provider == "custom":
        return OpenAICompatClient(
            model or "local-model",
            base_url or "http://127.0.0.1:5001/v1",
            api_key or os.environ.get("OPENAI_API_KEY") or "local",
        )
    known = ", ".join(sorted(OPENAI_COMPATIBLE_PRESETS)) + ", anthropic, or a full URL"
    raise LLMError(f"Unknown provider '{provider}'. Use one of: {known}.")
