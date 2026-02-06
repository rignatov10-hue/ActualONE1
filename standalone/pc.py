"""Opt-in local PC control for the standalone agent.

Disabled by default. The user grants control explicitly:
- env ``ANALYST_PC_TOOLS=1`` (or GUI checkbox / CLI ``--pc``).

When enabled, the model may use a fixed toolset - no code generation beyond
that: run_shell, list_dir, read_file, write_file, open_path, open_url. Every
invocation is appended to ``pc_log.jsonl`` inside the conversation memory
folder, so the owner can audit everything the agent did.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

MAX_OUTPUT_CHARS = 10000
DEFAULT_MAX_CHARS = 8000
MAX_MAX_CHARS = 50000
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_WRITE_CHARS = 200000
MAX_ENTRIES = 500
AUDIT_NAME = "pc_log.jsonl"

TRUE_SET = {"1", "true", "yes", "on", "да", "вкл"}


class PCError(Exception):
    """Raised when a PC tool cannot complete the request."""


def enabled() -> bool:
    return os.environ.get("ANALYST_PC_TOOLS", "").strip().lower() in TRUE_SET


def set_enabled(value: bool) -> None:
    os.environ["ANALYST_PC_TOOLS"] = "1" if value else "0"


def confirm_mode() -> str:
    """'ask' by default; 'auto' when the user disabled confirmations."""
    return "auto" if os.environ.get("ANALYST_PC_CONFIRM", "").strip().lower() in TRUE_SET else "ask"


def set_confirm_mode(auto: bool) -> None:
    os.environ["ANALYST_PC_CONFIRM"] = "1" if auto else ""


def _file_tools_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "run_shell",
            "description": (
                "Run a shell command on the user's PC and return its output. Use only what"
                " the user asked for; prefer read-only commands; ask before anything"
                " destructive or irreversible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout": {"type": "integer", "description": f"Seconds before it is killed (default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT})."},
                },
                "required": ["command"],
            },
        },
        {
            "name": "list_dir",
            "description": "List a folder on the user's PC: names, types, sizes (bounded).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Folder path; current folder when omitted."}},
            },
        },
        {
            "name": "read_file",
            "description": "Read the beginning of a text file on the user's PC (bounded).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path."},
                    "max_chars": {"type": "integer", "description": f"Characters to return (default {DEFAULT_MAX_CHARS}, max {MAX_MAX_CHARS})."},
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": (
                "Create or overwrite a text file on the user's PC (parents created). Only"
                " when the user asked for it; say the full path in the answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "open_path",
            "description": "Open a file or folder on the user's PC in the system default application.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File or folder to open."}},
                "required": ["path"],
            },
        },
        {
            "name": "web_read",
            "description": (
                "Fetch a public web page and return its readable text (tags and scripts"
                " stripped). Use for documentation, search results pages and articles."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http(s) URL to read."},
                    "max_chars": {"type": "integer", "description": f"Characters to return (default {DEFAULT_MAX_CHARS})."},
                },
                "required": ["url"],
            },
        },
        {
            "name": "open_url",
            "description": "Open an http(s) URL in the user's default browser.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to open."}},
                "required": ["url"],
            },
        },
    ]


PC_TOOL_SCHEMAS = _file_tools_schema()


def _trim(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text)} chars total)"
    return text


def _audit(conv: Any, record: dict[str, Any]) -> None:
    try:
        audit = Path(getattr(conv, "root", "memory")) / AUDIT_NAME
        audit.parent.mkdir(parents=True, exist_ok=True)
        with open(audit, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def run_shell(conv: Any, command: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    if not (command or "").strip():
        raise PCError("Empty command.")
    try:
        seconds = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=seconds,
            errors="replace",
        )
        result = {
            "command": command,
            "returncode": completed.returncode,
            "stdout": _trim(completed.stdout or ""),
            "stderr": _trim(completed.stderr or ""),
            "seconds": round(time.time() - started, 2),
        }
    except subprocess.TimeoutExpired:
        raise PCError(f"Command exceeded {seconds}s and was killed: {command!r}") from None
    except OSError as exc:
        raise PCError(f"Could not run command: {exc}") from None
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "run_shell", **result})
    return result


def list_dir(conv: Any, path: str = ".") -> dict[str, Any]:
    base = Path(path).expanduser()
    if not base.is_dir():
        raise PCError(f"Not a folder or does not exist: {base}")
    entries = []
    for item in sorted(base.iterdir())[:MAX_ENTRIES]:
        kind = "dir" if item.is_dir() else "file"
        size = item.stat().st_size if item.is_file() else None
        entries.append({"name": item.name, "type": kind, "size": size})
    result = {"path": str(base.resolve()), "count": len(entries), "entries": entries}
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "list_dir", "path": str(base)})
    return result


def read_file(conv: Any, path: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_file():
        raise PCError(f"Not a file or does not exist: {target}")
    try:
        limit = max(1, min(int(max_chars), MAX_MAX_CHARS))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_CHARS
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PCError(f"Could not read file: {exc}") from None
    result = {"path": str(target.resolve()), "size_chars": len(text), "content": _trim(text, limit)}
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "read_file", "path": str(target)})
    return result


def write_file(conv: Any, path: str, content: str) -> dict[str, Any]:
    if len(content or "") > MAX_WRITE_CHARS:
        raise PCError(f"Content too large ({len(content)} chars, max {MAX_WRITE_CHARS}).")
    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or "", encoding="utf-8")
    except OSError as exc:
        raise PCError(f"Could not write file: {exc}") from None
    result = {"path": str(target.resolve()), "written_chars": len(content or "")}
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "write_file", "path": str(target), "chars": len(content or "")})
    return result


def web_read(conv: Any, url: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    import re as _re
    from urllib.request import Request as _Request, urlopen as _urlopen

    clean = (url or "").strip()
    if not clean.lower().startswith(("http://", "https://")):
        raise PCError("Only http(s) URLs can be read.")
    try:
        limit = max(200, min(int(max_chars), MAX_MAX_CHARS))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_CHARS
    req = _Request(clean, headers={"User-Agent": "Mozilla/5.0 (standalone analyst agent)"})
    try:
        with _urlopen(req, timeout=20) as response:
            raw = response.read(2_000_000)
    except Exception as exc:
        raise PCError(f"Could not fetch URL: {exc}") from None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception as exc:
        raise PCError(f"Could not decode page: {exc}") from None
    text = _re.sub(r"<script\b.*?</script>", " ", text, flags=_re.I | _re.S)
    text = _re.sub(r"<style\b.*?</style>", " ", text, flags=_re.I | _re.S)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    result = {"url": clean, "length": len(text), "content": _trim(text, limit)}
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "web_read", "url": clean})
    return result


def _launch(target: str) -> None:
    if hasattr(os, "startfile"):
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", target], check=False)
    else:
        subprocess.run(["xdg-open", target], check=False)


def open_path(conv: Any, path: str) -> dict[str, Any]:
    target = Path(path).expanduser()
    if not target.exists():
        raise PCError(f"Path does not exist: {target}")
    _launch(str(target))
    result = {"opened": str(target.resolve())}
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "open_path", "path": str(target)})
    return result


def open_url(conv: Any, url: str) -> dict[str, Any]:
    if not (url or "").strip().lower().startswith(("http://", "https://")):
        raise PCError("Only http(s) URLs can be opened.")
    webbrowser.open(url)
    result = {"opened": url}
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "open_url", "url": url})
    return result


PC_IMPLS: dict[str, Callable[..., dict[str, Any]]] = {
    "run_shell": run_shell,
    "list_dir": list_dir,
    "read_file": read_file,
    "write_file": write_file,
    "open_path": open_path,
    "open_url": open_url,
    "web_read": web_read,
}

PC_SECTION = """

Local PC tools (the user enables them explicitly, every action is audit-logged in
pc_log.jsonl): run_shell, list_dir, read_file, write_file, open_path, open_url,
web_read.
Rules: run only what the task needs; prefer read-only commands; announce in the
answer what you did on the PC; before anything destructive or irreversible, call
request_approval and wait for the user's yes; never send local data or secrets
to the network.
"""

PC_SECTION_AUTO = """

Local PC tools (the user enables them explicitly, every action is audit-logged in
pc_log.jsonl): run_shell, list_dir, read_file, write_file, open_path, open_url,
web_read.
Rules: run only what the task needs; the user has disabled confirmation prompts,
so proceed decisively with the approved task and describe in your answer exactly
what you did; still never send local data or secrets to the network.
"""


__all__ = [
    "PCError",
    "PC_IMPLS",
    "PC_SECTION",
    "PC_TOOL_SCHEMAS",
    "enabled",
    "run_shell",
    "set_enabled",
]
