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
import re
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
MAX_REPLACE_CHARS = 500000
MAX_SEARCH_RESULTS = 200
MAX_DRAW_CMDS = 500
MAX_DRAW_SIZE = 4096
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "AppData", "$RECYCLE.BIN", "Windows"}
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


def desktop_dir() -> Path:
    """The real Desktop folder, including Windows OneDrive redirection."""
    home = Path.home()
    if os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except Exception:
            pass
    for candidate in (home / "Desktop", home / "OneDrive" / "Desktop", home / "Рабочий стол"):
        if candidate.is_dir():
            return candidate
    return home / "Desktop"


DESKTOP_FIRST = {"desktop", "рабочий стол", "рабочем столе", "рабочего стола"}


def _draw_px(value: Any, size: int) -> float:
    """Map a 0-1000 grid coordinate to pixels; larger values are absolute pixels."""
    try:
        coord = float(value)
    except (TypeError, ValueError):
        raise PCError(f"Invalid coordinate value: {value!r}") from None
    if coord > 1000:
        return max(0.0, min(coord, float(size)))
    return max(0.0, min(coord, 1000.0)) * size / 1000.0


def smart_path(path: str) -> Path:
    """Resolve '~', 'Desktop/x', 'рабочий стол/x' and relative names deterministically."""
    text = (path or "").strip().strip('"').strip("'")
    if not text:
        raise PCError("Empty path.")
    expanded = Path(text).expanduser()
    if expanded.is_absolute():
        return expanded
    first = expanded.parts[0].lower().strip("\\/")
    if first in DESKTOP_FIRST or first.startswith("desktop"):
        return desktop_dir().joinpath(*expanded.parts[1:])
    return Path.home().joinpath(*expanded.parts)


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
                    "path": {"type": "string", "description": "File path: absolute, '~/...', 'Desktop/<name>' (the real Desktop), or relative to the user profile."},
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
                    "path": {"type": "string", "description": "File path: absolute, '~/...', 'Desktop/<name>' (the real Desktop), or relative to the user profile."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "replace_in_file",
            "description": (
                "Edit an existing text file on the user's PC by replacing an exact"
                " fragment - safer than rewriting the whole file. Copy old_text"
                " verbatim from read_file output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path: absolute, '~/...', 'Desktop/<name>' (the real Desktop), or relative to the user profile."},
                    "old_text": {"type": "string", "description": "Exact existing fragment to replace (copied verbatim from read_file)."},
                    "new_text": {"type": "string", "description": "Replacement fragment (may be empty to delete)."},
                    "all_occurrences": {"type": "boolean", "description": "Replace every occurrence; default replaces exactly one."},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
        {
            "name": "search_files",
            "description": (
                "Recursively search text files in a folder (default: the user profile)"
                " for a case-insensitive substring. Find where something lives before"
                " editing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Case-insensitive text to search for."},
                    "path": {"type": "string", "description": "Folder to search; the user profile when omitted."},
                    "extension": {"type": "string", "description": "Only files with this extension, e.g. 'py' or 'txt' (optional)."},
                    "max_results": {"type": "integer", "description": "Maximum matches to return (default 50, max 200)."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "open_path",
            "description": "Open a file or folder on the user's PC in the system default application.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File or folder to open: absolute, '~/...', 'Desktop/<name>', or relative to the user profile."}},
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
            "name": "draw_image",
            "description": (
                "Draw a PNG picture on the user's PC from primitive commands (rect,"
                " ellipse, line, polygon, text) on a 0-1000 coordinate grid. Good for"
                " logos, schemas, charts-as-pictures and simple art; open the result"
                " with open_path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Output PNG path: absolute, '~/...', 'Desktop/<name>' (the real Desktop), or relative to the user profile. '.png' is appended automatically."},
                    "commands": {"type": "string", "description": "JSON array of primitives: {\"shape\":\"rect\"|\"ellipse\"|\"line\",\"xy\":[x1,y1,x2,y2]}, {\"shape\":\"polygon\",\"points\":[x1,y1,x2,y2,x3,y3,...]}, {\"shape\":\"text\",\"xy\":[x,y],\"text\":\"...\",\"size\":24}. Every object supports \"fill\" (CSS color like '#2233ff'), rect/ellipse/line also \"outline\" and \"width\"."},
                    "width": {"type": "integer", "description": "Image width in pixels (default 1024, max 4096)."},
                    "height": {"type": "integer", "description": "Image height in pixels (default 1024, max 4096)."},
                    "background": {"type": "string", "description": "Background CSS color (default '#ffffff')."},
                },
                "required": ["path", "commands"],
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


DESTRUCTIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"rm\s+(-{1,2}\w+\s+)*-{1,2}[\w]*[rf][\w]*(\s+-{1,2}\w+)*\s+\"?(/|~/|\$HOME|\*)", "recursive delete of a system location"),
    (r"\bmkfs\b", "disk formatting (mkfs)"),
    (r"\bdd\b[^;|&]*\bof=/dev/(sd|nvme|hd|disk)", "raw disk write via dd"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "host shutdown/reboot"),
    (r"\bformat\s+[a-z]:", "disk formatting (format x:)"),
    (r"(\bdel\b|\brd\b|\brmdir\b)\s+/[sqr]+\s+[^;|&]*[a-z]:\\(windows|program files|users\\[a-z]+\$?)(\s|$)", "recursive delete of a system folder"),
    (r"\breg\s+delete\s+hklm", "deleting HKLM registry keys"),
    (r"\bvssadmin\s+delete\s+shadows", "deleting shadow copies"),
    (r"\bbcdedit\b", "boot configuration change"),
    (r"\bdiskpart\b", "disk partitioning (diskpart)"),
    (r"\bcipher\s+/w", "disk wipe (cipher /w)"),
    (r":\(\)\{\s*:\|:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r">\s*/dev/(sd|nvme|hd)", "raw device overwrite"),
    (r"Remove-Item[^;|&]*-Recurse[^;|&]*[a-z]:\\(Windows|Program Files)\b", "recursive delete of a system folder"),
)
GUARD_ERROR = (
    "Blocked by the local safety filter: {reason}. The user's PC host is protected;"
    " if this exact action is truly required, stop and ask the user to run it manually."
)


def guard_command(command: str) -> None:
    low = (command or "").lower()[:4000]
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, low):
            raise PCError(GUARD_ERROR.format(reason=reason))


def run_shell(conv: Any, command: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    if not (command or "").strip():
        raise PCError("Empty command.")
    guard_command(command)
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
    base = Path.home() if str(path).strip() in ("", ".") else smart_path(path)
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
    target = smart_path(path)
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
    target = smart_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or "", encoding="utf-8")
    except OSError as exc:
        raise PCError(f"Could not write file: {exc}") from None
    result = {"path": str(target.resolve()), "written_chars": len(content or "")}
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "write_file", "path": str(target), "chars": len(content or "")})
    return result


def replace_in_file(
    conv: Any, path: str, old_text: str, new_text: str, all_occurrences: bool = False
) -> dict[str, Any]:
    target = smart_path(path)
    if not target.is_file():
        raise PCError(f"Not a file or does not exist: {target}")
    if not old_text:
        raise PCError("old_text is empty.")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PCError(f"Could not read file: {exc}") from None
    if len(text) > MAX_REPLACE_CHARS:
        raise PCError(f"File too large ({len(text)} chars, max {MAX_REPLACE_CHARS}); use write_file or run_shell instead.")
    count = text.count(old_text)
    if count == 0:
        raise PCError("old_text not found in the file - read_file first and copy the exact fragment.")
    if count > 1 and not all_occurrences:
        raise PCError(
            f"old_text occurs {count} times; include more surrounding context or pass all_occurrences=true."
        )
    updated = text.replace(old_text, new_text) if all_occurrences else text.replace(old_text, new_text, 1)
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        raise PCError(f"Could not write file: {exc}") from None
    done = count if all_occurrences else 1
    result = {"path": str(target.resolve()), "replacements": done}
    _audit(
        conv,
        {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "replace_in_file", "path": str(target), "replacements": done},
    )
    return result


def search_files(
    conv: Any, query: str, path: str = "", extension: str = "", max_results: int = 50
) -> dict[str, Any]:
    needle = (query or "").strip().lower()
    if not needle:
        raise PCError("Empty query.")
    base = Path.home() if str(path).strip() in ("", ".") else smart_path(path)
    if not base.is_dir():
        raise PCError(f"Not a folder or does not exist: {base}")
    try:
        limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    except (TypeError, ValueError):
        limit = 50
    ext = (extension or "").strip().lstrip(".").lower()
    matches: list[dict[str, Any]] = []
    scanned = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if len(matches) >= limit:
                break
            if ext and not name.lower().endswith("." + ext):
                continue
            candidate = Path(root) / name
            try:
                if candidate.stat().st_size > 5_000_000:
                    continue
                with open(candidate, "r", encoding="utf-8", errors="ignore") as handle:
                    for line_no, line in enumerate(handle, 1):
                        if needle in line.lower():
                            matches.append({"file": str(candidate), "line": line_no, "text": line.strip()[:200]})
                            if len(matches) >= limit:
                                break
                scanned += 1
            except OSError:
                continue
        if len(matches) >= limit:
            break
    result = {"query": query, "base": str(base), "files_scanned": scanned, "matches": len(matches), "results": matches}
    _audit(
        conv,
        {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "search_files", "query": query, "base": str(base)},
    )
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


def _draw_font(size: int) -> Any:
    from PIL import ImageFont

    for name in ("arial.ttf", "consolas.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _apply_draw_command(draw: Any, cmd: Any, index: int, width: int, height: int) -> None:
    if not isinstance(cmd, dict):
        raise PCError(f"Command #{index} is not an object.")
    shape = str(cmd.get("shape") or cmd.get("type") or "").strip().lower()
    fill = cmd.get("fill") or cmd.get("color") or "#000000"
    xy = cmd.get("xy") or cmd.get("coords") or cmd.get("box")
    try:
        if shape == "rect" or shape == "rectangle":
            if not isinstance(xy, (list, tuple)) or len(xy) != 4:
                raise PCError(f"Command #{index} ('rect') needs xy = [x1, y1, x2, y2].")
            box = [
                _draw_px(xy[0], width),
                _draw_px(xy[1], height),
                _draw_px(xy[2], width),
                _draw_px(xy[3], height),
            ]
            draw.rectangle(box, fill=fill, outline=cmd.get("outline") or fill, width=int(cmd.get("width", 1) or 1))
        elif shape == "ellipse" or shape == "circle":
            if not isinstance(xy, (list, tuple)) or len(xy) != 4:
                raise PCError(f"Command #{index} ('ellipse') needs xy = [x1, y1, x2, y2].")
            box = [
                _draw_px(xy[0], width),
                _draw_px(xy[1], height),
                _draw_px(xy[2], width),
                _draw_px(xy[3], height),
            ]
            draw.ellipse(box, fill=fill, outline=cmd.get("outline") or fill, width=int(cmd.get("width", 1) or 1))
        elif shape == "line":
            if not isinstance(xy, (list, tuple)) or len(xy) != 4:
                raise PCError(f"Command #{index} ('line') needs xy = [x1, y1, x2, y2].")
            draw.line(
                [_draw_px(xy[0], width), _draw_px(xy[1], height), _draw_px(xy[2], width), _draw_px(xy[3], height)],
                fill=fill,
                width=max(1, int(cmd.get("width", 2) or 2)),
            )
        elif shape == "polygon":
            points = cmd.get("points") or xy
            if not isinstance(points, (list, tuple)) or len(points) < 6:
                raise PCError(f"Command #{index} ('polygon') needs points = [x1, y1, x2, y2, x3, y3, ...].")
            flat: list[float] = []
            for value in points:
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    flat.extend([_draw_px(value[0], width), _draw_px(value[1], height)])
                else:
                    flat.append(_draw_px(value, width))
            if len(flat) % 2 or len(flat) < 6:
                raise PCError(f"Command #{index} ('polygon') has unpaired coordinates.")
            draw.polygon(list(zip(flat[0::2], flat[1::2])), fill=fill, outline=cmd.get("outline") or fill)
        elif shape == "text":
            message = str(cmd.get("text", ""))
            if not message:
                raise PCError(f"Command #{index} ('text') has empty text.")
            if not isinstance(xy, (list, tuple)) or len(xy) != 2:
                raise PCError(f"Command #{index} ('text') needs xy = [x, y].")
            size = max(6, min(int(cmd.get("size", 24) or 24), 400))
            draw.text(
                (_draw_px(xy[0], width), _draw_px(xy[1], height)),
                message,
                fill=fill,
                font=_draw_font(size),
            )
        else:
            raise PCError(
                f"Command #{index}: unknown shape {shape!r}; use rect, ellipse, line, polygon or text."
            )
    except PCError:
        raise
    except (ValueError, TypeError, OSError) as exc:
        raise PCError(f"Command #{index} failed: {exc}") from None


def draw_image(
    conv: Any,
    path: str,
    commands: Any,
    width: int = 1024,
    height: int = 1024,
    background: str = "#ffffff",
) -> dict[str, Any]:
    """Render a PNG picture on the user's PC from primitive drawing commands."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise PCError("Pillow is not installed - run: pip install pillow") from None
    if isinstance(commands, str):
        try:
            commands = json.loads(commands)
        except json.JSONDecodeError:
            raise PCError("commands is not valid JSON - pass a JSON array of objects.") from None
    if not isinstance(commands, list) or not commands:
        raise PCError("commands must be a non-empty JSON array of primitive objects.")
    if len(commands) > MAX_DRAW_CMDS:
        raise PCError(f"Too many commands ({len(commands)}, max {MAX_DRAW_CMDS}).")
    try:
        w = max(16, min(int(width), MAX_DRAW_SIZE))
        h = max(16, min(int(height), MAX_DRAW_SIZE))
    except (TypeError, ValueError):
        w = h = 1024
    target = smart_path(path)
    if target.suffix.lower() != ".png":
        target = target.with_suffix(".png")
    try:
        image = Image.new("RGB", (w, h), background)
    except (ValueError, TypeError):
        raise PCError(f"Invalid background color: {background!r}") from None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PCError(f"Could not create folder: {exc}") from None
    draw = ImageDraw.Draw(image)
    for index, cmd in enumerate(commands):
        _apply_draw_command(draw, cmd, index, w, h)
    try:
        image.save(target, "PNG")
    except (OSError, ValueError) as exc:
        raise PCError(f"Could not save image: {exc}") from None
    result = {"path": str(target.resolve()), "size": [w, h], "shapes": len(commands)}
    _audit(
        conv,
        {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "draw_image", "path": str(target), "shapes": len(commands)},
    )
    return result


def _launch(target: str) -> None:
    if hasattr(os, "startfile"):
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", target], check=False)
    else:
        subprocess.run(["xdg-open", target], check=False)


def open_path(conv: Any, path: str) -> dict[str, Any]:
    target = smart_path(path)
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
    "replace_in_file": replace_in_file,
    "search_files": search_files,
    "open_path": open_path,
    "open_url": open_url,
    "web_read": web_read,
    "draw_image": draw_image,
}

PC_SECTION = """

Local PC tools (the user enables them explicitly, every action is audit-logged in
pc_log.jsonl): run_shell, list_dir, read_file, write_file, replace_in_file,
search_files, open_path, open_url, web_read, draw_image.
Work like an autonomous coding agent: locate (search_files, list_dir) -> read
(read_file) -> modify (replace_in_file for targeted edits, write_file for new
or fully rewritten files) -> run/verify (run_shell) -> report the result. Never
end with 'do it yourself' instructions when a tool can do it.
draw_image renders a PNG picture from primitive commands (rect/ellipse/line/
polygon/text, coordinates on a 0-1000 grid) - draw logos, schemas and simple
art to 'Desktop/<name>' and open the file with open_path so the user sees it.
A local safety filter hard-blocks destructive system commands (format, shutdown,
disk writes, deletes of system folders) regardless of approvals.
Rules: run only what the task needs; prefer read-only commands; announce in the
answer what you did on the PC; before anything destructive or irreversible, call
request_approval and wait for the user's yes; never send local data or secrets
to the network.
Paths: 'Desktop/<name>' or 'рабочий стол/<name>' resolves to the real Desktop
automatically - so creating a file the user asked for is: write_file with path
'Desktop/<name>' and full content, then report the resulting path from the result.
When the task includes code to run, write the script with write_file and run it via
run_shell (e.g. 'python "C:/Users/x/Desktop/script.py"') - do not leave manual
instructions for the user when run_shell can do it.
"""

PC_SECTION_AUTO = """

Local PC tools (the user enables them explicitly, every action is audit-logged in
pc_log.jsonl): run_shell, list_dir, read_file, write_file, replace_in_file,
search_files, open_path, open_url, web_read, draw_image.
Work like an autonomous coding agent: locate (search_files, list_dir) -> read
(read_file) -> modify (replace_in_file for targeted edits, write_file for new
or fully rewritten files) -> run/verify (run_shell) -> report the result. Never
end with manual 'do it yourself' steps when a tool can do it.
A local safety filter hard-blocks destructive system commands (format, shutdown,
disk writes, deletes of system folders) regardless of the no-confirm mode.
Rules: run only what the task needs; the user has disabled confirmation prompts,
so proceed decisively with the approved task and describe in your answer exactly
what you did; still never send local data or secrets to the network.
Paths: 'Desktop/<name>' or 'рабочий стол/<name>' resolves to the real Desktop
automatically - write files there with write_file ('Desktop/<name>') and report
the resulting path. When code must be executed, write the script with write_file
and run it via run_shell ('python <path>') - do not give the user manual steps
when run_shell can do it.
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
