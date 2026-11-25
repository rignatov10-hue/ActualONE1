"""The agent loop: fixed tools only, persistent memory, any model back-end.

Advanced runtime:
- JSONL tracing of every tool call and model step (memory/traces/<name>.jsonl);
- human-in-the-loop: the request_approval tool asks the user before any
  destructive, irreversible or externally visible action;
- rolling summarization: long conversations are compressed into a summary so
  the context stays small;
- long-term facts: the save_fact tool persists user facts across conversations
  (memory/facts.json), injected into every turn.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from . import pc as pc_mod
from .llm import AnthropicClient, OpenAICompatClient
from .memory import Conversation, facts_section, load_facts, save_fact
from .pc import PCError
from .skills import (
    SKILL_TOOL_SCHEMA,
    SkillError,
    TOOL_NAME,
    build_section,
    discover_skills,
    run_load_skill,
)
from .tools import TOOL_IMPLS, TOOL_SCHEMAS, ToolError, utc_now

INSTRUCTIONS = """You are a conversational data analyst and a general assistant. When the user asks
about data, they give a source - a Google Drive link to a CSV/Excel/Google Sheet, an https URL,
or a local file path - and asks questions in plain language. If the current question is not about
data, just answer it; never demand a dataset or repeat requests to send a CSV. Your memory is
durable: everything loaded or discussed earlier in this conversation still holds; never ask for
anything already given.

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

Your capabilities are fixed tools only: the five data tools, load_skill for reading user skills,
and - only when listed in these instructions - the user-enabled local PC tools. You cannot write
and run arbitrary code, use shells beyond the listed PC tools, or fetch the web beyond open_url.
Stay within the tools and never promise otherwise.

SAFETY - human in the loop: right before any destructive, irreversible, or externally visible
action (delete, overwrite, send, publish, install, change settings) call request_approval with a
short question and proceed only when it returns approved=true. When approval is false or there
is no way to confirm, stop and tell the user what you intended. Never split one destructive
action into several smaller ones to dodge this rule.

MEMORY: use save_fact when the user shares a durable preference or project fact (name, team,
naming conventions, default settings); it survives across conversations. Do not save transient
data facts that belong to the loaded dataset.
"""

APPROVAL_TOOL_SCHEMA: dict[str, Any] = {
    "name": "request_approval",
    "description": (
        "Ask the user to explicitly confirm a destructive, irreversible, or externally visible"
        " action (deleting, overwriting, sending, publishing, installing). Call it immediately"
        " before such an action; proceed only when approved is true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Short concrete question about exactly what will be done.",
            }
        },
        "required": ["question"],
    },
}

SAVE_FACT_TOOL_SCHEMA: dict[str, Any] = {
    "name": "save_fact",
    "description": (
        "Persist a durable user fact or preference across conversations (project names,"
        " conventions, settings). Overwrites the fact with the same key; empty value deletes it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Short stable key, e.g. 'user.name'."},
            "value": {"type": "string", "description": "Fact content; empty string deletes the key."},
        },
        "required": ["key", "value"],
    },
}

SUMMARY_INSTRUCTION = (
    "Compress this conversation part into a dense summary in Russian: what file was loaded, the"
    " current narrowed scope, decisions, user preferences, terminology, and open questions."
    " Keep all numbers and facts; no polite phrases. A few short paragraphs maximum."
)

SUMMARY_AFTER = 40
SUMMARY_KEEP = 12


def _trace(conv: Conversation, event: dict[str, Any]) -> None:
    try:
        directory = conv.root / "traces"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{conv.name}.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({**event, "time": utc_now()}, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _notify(conv: Conversation, text: str) -> None:
    hook = getattr(conv, "activity_hook", None)
    if hook:
        try:
            hook(text)
        except Exception:
            pass


def _execute(conv: Conversation, call: dict[str, Any]) -> str:
    started = time.time()
    content = _execute_impl(conv, call)
    _trace(
        conv,
        {
            "event": "tool",
            "tool": call.get("name", ""),
            "arguments": call.get("arguments") or {},
            "seconds": round(time.time() - started, 2),
            "result": content[:2000],
        },
    )
    return content


def _execute_impl(conv: Conversation, call: dict[str, Any]) -> str:
    name = call.get("name", "")
    arguments = call.get("arguments") or {}
    if name == TOOL_NAME:
        try:
            return json.dumps(run_load_skill(arguments.get("name", "")), ensure_ascii=False, default=str)
        except SkillError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        except TypeError as exc:
            return json.dumps({"error": f"Bad arguments for '{name}': {exc}."}, ensure_ascii=False)
    if name == "request_approval":
        return _request_approval(conv, arguments)
    if name == "save_fact":
        return _save_fact(conv, arguments)
    if name in pc_mod.PC_IMPLS:
        if not pc_mod.enabled():
            return json.dumps(
                {"error": "PC tools are disabled. The user must enable 'Управление ПК' in the settings first."},
                ensure_ascii=False,
            )
        try:
            result = pc_mod.PC_IMPLS[name](conv, **arguments)
            return json.dumps(result, ensure_ascii=False, default=str)
        except PCError as exc:
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


def _request_approval(conv: Conversation, arguments: dict[str, Any]) -> str:
    question = (arguments.get("question") or "").strip()
    if not question:
        return json.dumps({"error": "Describe what needs confirmation."}, ensure_ascii=False)
    if pc_mod.confirm_mode() == "auto":
        return json.dumps(
            {
                "approved": True,
                "auto": True,
                "question": question,
                "note": "the user disabled confirmations; the action is audit-logged",
            },
            ensure_ascii=False,
        )
    hook: Callable[[str], bool] | None = getattr(conv, "approval_hook", None)
    if hook is None:
        return json.dumps(
            {"approved": False, "note": "no confirmation channel in this session; refuse the action"},
            ensure_ascii=False,
        )
    _notify(conv, f"ЖДЁТ ПОДТВЕРЖДЕНИЯ: {question}")
    try:
        approved = bool(hook(question))
    except Exception as exc:
        return json.dumps({"approved": False, "error": str(exc)}, ensure_ascii=False)
    return json.dumps(
        {"approved": approved, "question": question},
        ensure_ascii=False,
    )


def _save_fact(conv: Conversation, arguments: dict[str, Any]) -> str:
    key = (arguments.get("key") or "").strip()[:64]
    value = str(arguments.get("value") or "").strip()[:2000]
    if not key:
        return json.dumps({"error": "Provide a fact key."}, ensure_ascii=False)
    try:
        current = save_fact(conv.root, key, value)
    except OSError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps(
        {"saved": key, "total_facts": len(current), "deleted": not value},
        ensure_ascii=False,
    )


def _system_state() -> tuple[str, list[dict[str, Any]]]:
    """Instructions and tool schemas for this turn: skills and PC tools are re-read every turn."""
    try:
        skills = discover_skills()
    except OSError:
        skills = []
    instructions = INSTRUCTIONS
    schemas = list(TOOL_SCHEMAS)
    if skills:
        instructions += build_section(skills)
        schemas.append(SKILL_TOOL_SCHEMA)
    if pc_mod.enabled():
        instructions += pc_mod.PC_SECTION_AUTO if pc_mod.confirm_mode() == "auto" else pc_mod.PC_SECTION
        schemas.extend(pc_mod.PC_TOOL_SCHEMAS)
    schemas.append(APPROVAL_TOOL_SCHEMA)
    schemas.append(SAVE_FACT_TOOL_SCHEMA)
    return instructions, schemas


def _maybe_summarize(conv: Conversation, client: Any) -> None:
    total = len(conv.model_messages())
    if total <= SUMMARY_AFTER:
        return
    keep = SUMMARY_KEEP
    if any(event.get("role") == "summary" for event in conv.model_messages()[-(keep + 2) :]):
        return
    tail = conv.model_messages()[: total - keep]
    try:
        response = client.complete(
            "You summarize analyst conversations; answer with the summary text only.",
            [
                {"role": "user", "content": SUMMARY_INSTRUCTION},
                {
                    "role": "user",
                    "content": json.dumps(tail[-SUMMARY_AFTER:], ensure_ascii=False, default=str),
                },
            ],
            [],
        )
    except Exception:
        return
    summary_text = (response.get("content") or "").strip()
    if not summary_text:
        return
    conv.condense(summary_text, keep=keep)
    _trace(conv, {"event": "summarize", "compressed": len(tail)})


def run_turn(conv: Conversation, client: Any, user_text: str, max_steps: int = 15) -> str:
    """One user turn: run the tool loop until the model produces a final answer."""
    conv.append({"role": "user", "content": user_text, "time": utc_now()})
    instructions, schemas = _system_state()
    _maybe_summarize(conv, client)
    summary = conv.latest_summary()
    if summary:
        instructions += f"\nSummary of the earlier part of this dialog:\n{summary}\n"
    facts = load_facts(conv.root)
    if facts:
        instructions += facts_section(facts)
    messages = conv.model_messages()
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
            content = _execute(conv, call)
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
    conv.approval_hook = _console_approval
    conv.activity_hook = lambda text: print(f"--- {text}")
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


def _console_approval(question: str) -> bool:
    try:
        return input(f"ПОДТВЕРЖДЕНИЕ: {question} [y/n]: ").strip().lower() in {"y", "yes", "д", "да"}
    except (EOFError, KeyboardInterrupt):
        return False


def make_client(provider: str, model: str | None, base_url: str | None, api_key: str | None) -> Any:
    from .llm import build_client

    return build_client(provider, model, base_url, api_key)


execute_tool = _execute

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
