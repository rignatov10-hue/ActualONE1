"""The agent loop: fixed tools only, persistent memory, any model back-end."""

from __future__ import annotations

import json
from typing import Any

from .llm import AnthropicClient, OpenAICompatClient
from .memory import Conversation
from .skills import (
    SKILL_TOOL_SCHEMA,
    SkillError,
    TOOL_NAME,
    build_section,
    discover_skills,
    run_load_skill,
)
from .tools import TOOL_IMPLS, TOOL_SCHEMAS, ToolError, utc_now

INSTRUCTIONS = """You are a conversational data analyst. The user gives you a data source - a
Google Drive link to a CSV/Excel/Google Sheet, an https URL, or a local file path - and asks
questions in plain language. Your memory is durable: everything loaded or discussed earlier in
this conversation still holds; never ask for anything already given.

How to work:

1. To load data call load_csv_from_drive with the source. It returns the file title, row count,
   and column list with types, but no rows. Loading a new file replaces the previous one.
2. Before answering, call describe_columns to learn the real column names, types and value
   distributions. Never invent column names.
3. Answer every question with tool results: describe_columns for distributions, filter_rows for
   matching rows (set set_as_active when the user narrows the subject, e.g. "now only EU
   customers"), group_by_aggregate for totals, averages, counts and rankings by category, and
   reset_scope when the user asks about all rows again. Chain steps when one is not enough.
4. Every number you state must come from a tool result in this conversation. If the fixed tools
   cannot answer a question (free text judgment, cross-file joins, statistics beyond the listed
   aggregates), say so plainly and offer the closest available analysis.
5. Be concise and conversational. Report real numbers with units, name the columns used, state
   the row count any percentage is based on, and round sensibly.
6. If a question mentions a column that does not exist, list available columns instead of
   guessing.

Your only capabilities are the five fixed tools plus load_skill for reading user skills. You
cannot run code, open a shell, edit files, or fetch the web. Stay within the tools and never
promise otherwise.
"""


def _system_state() -> tuple[str, list[dict[str, Any]]]:
    """Instructions and tool schemas for this turn: skills are re-read every turn."""
    try:
        skills = discover_skills()
    except OSError:
        skills = []
    if not skills:
        return INSTRUCTIONS, list(TOOL_SCHEMAS)
    return INSTRUCTIONS + build_section(skills), list(TOOL_SCHEMAS) + [SKILL_TOOL_SCHEMA]


def execute_tool(conv: Conversation, call: dict[str, Any]) -> str:
    name = call.get("name", "")
    arguments = call.get("arguments") or {}
    if name == TOOL_NAME:
        try:
            return json.dumps(run_load_skill(arguments.get("name", "")), ensure_ascii=False, default=str)
        except SkillError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        except TypeError as exc:
            return json.dumps({"error": f"Bad arguments for '{name}': {exc}."}, ensure_ascii=False)
    implementation = TOOL_IMPLS.get(name)
    if implementation is None:
        return json.dumps({"error": f"Unknown tool '{name}'."}, ensure_ascii=False)
    try:
        result = implementation(conv, **arguments)
        return json.dumps(result, ensure_ascii=False, default=str)
    except ToolError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except TypeError as exc:
        return json.dumps({"error": f"Bad arguments for '{name}': {exc}."}, ensure_ascii=False)


def run_turn(conv: Conversation, client: Any, user_text: str, max_steps: int = 15) -> str:
    """One user turn: run the tool loop until the model produces a final answer."""
    conv.append({"role": "user", "content": user_text, "time": utc_now()})
    instructions, schemas = _system_state()
    messages = list(conv.messages)
    for _ in range(max_steps):
        response = client.complete(instructions, messages, schemas)
        assistant: dict[str, Any] = {"role": "assistant", "content": response.get("content")}
        if response.get("tool_calls"):
            assistant["tool_calls"] = response["tool_calls"]
        conv.append(assistant)
        messages.append(assistant)
        if not response.get("tool_calls"):
            return response.get("content") or ""
        for call in response["tool_calls"]:
            content = execute_tool(conv, call)
            tool_event = {
                "role": "tool",
                "tool_call_id": call.get("id") or call.get("name", "tool"),
                "name": call.get("name", "tool"),
                "content": content,
            }
            conv.append(tool_event)
            messages.append(tool_event)
    fallback = {
        "role": "assistant",
        "content": (
            "I stopped before finishing: too many steps. Ask a narrower question or say 'continue'."
        ),
    }
    conv.append(fallback)
    return fallback["content"]


def chat(conv: Conversation, client: Any) -> None:
    print(f"Conversation '{conv.name}' | memory: {conv.root}")
    if conv.dataset is not None:
        print(f"Loaded earlier: {conv.dataset.title} ({int(conv.full.shape[0])} rows)")
    print("Empty line or 'exit' quits.\n")
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text or text.lower() in {"exit", "quit", "выход"}:
            break
        print()
        print(run_turn(conv, client, text))
        print()


def make_client(provider: str, model: str | None, base_url: str | None, api_key: str | None) -> Any:
    from .llm import build_client

    return build_client(provider, model, base_url, api_key)


__all__ = [
    "INSTRUCTIONS",
    "Conversation",
    "AnthropicClient",
    "OpenAICompatClient",
    "chat",
    "execute_tool",
    "make_client",
    "run_turn",
]
