"""Interactive browser control for the standalone agent.

Uses Playwright (Chromium). The import is lazy: the tools work even when
Playwright is not installed - they then return a clear error telling the user
what to install. Gated by the same ANALYST_PC_TOOLS switch as the other PC
tools; every action is appended to the pc_log.jsonl audit trail.
Set ANALYST_BROWSER_HEADED=1 to see the browser window instead of headless.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .pc import PCError, _audit, smart_path

DEFAULT_SNIPPET_CHARS = 6000
MAX_SNIPPET_CHARS = 20000

BROWSER_IMPLS: dict[str, Any] = {}
BROWSER_TOOLS = (
    "browser_open",
    "browser_click",
    "browser_type",
    "browser_snapshot",
    "browser_screenshot",
    "browser_close",
)

_state: dict[str, Any] = {}


def _headed() -> bool:
    return os.environ.get("ANALYST_BROWSER_HEADED", "").strip().lower() in {"1", "true", "yes", "on", "да", "вкл"}


def _profile_dir(conv: Any) -> Path:
    """Persistent Chromium profile dir so logins and cookies survive restarts."""
    override = os.environ.get("ANALYST_BROWSER_PROFILE", "").strip()
    if override:
        return Path(override)
    try:
        return Path(conv.root).parent / "browser_profile"
    except Exception:
        return Path("browser_profile").resolve()


def _page(conv: Any = None) -> Any:
    page = _state.get("page")
    if page is not None:
        return page
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PCError(
            "Playwright is not installed in this Python. Run in a terminal:"
            " pip install playwright && playwright install chromium"
        ) from exc
    pw = sync_playwright().start()
    profile = _profile_dir(conv)
    try:
        profile.mkdir(parents=True, exist_ok=True)
        context = pw.chromium.launch_persistent_context(
            str(profile), headless=not _headed(), viewport={"width": 1280, "height": 800}
        )
        browser_obj = None
    except Exception:
        browser_obj = pw.chromium.launch(headless=not _headed())
        context = browser_obj.new_context(viewport={"width": 1280, "height": 800})
    page = context.pages[0] if getattr(context, "pages", None) else context.new_page()
    _state.update({"pw": pw, "browser": browser_obj, "context": context, "page": page})
    return page


def _stop() -> None:
    try:
        if _state.get("browser"):
            _state["browser"].close()
        if _state.get("context"):
            _state["context"].close()
        if _state.get("pw"):
            _state["pw"].stop()
    except Exception:
        pass
    _state.clear()


def _log(conv: Any, tool: str, **extra: Any) -> None:
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, **extra})


def _visible_text(page: Any, limit: int) -> str:
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        text = ""
    return (text or "").strip()[:limit]


def browser_open(conv: Any, url: str, max_chars: int = DEFAULT_SNIPPET_CHARS) -> dict[str, Any]:
    clean = (url or "").strip()
    if not clean.lower().startswith(("http://", "https://")):
        raise PCError("Only http(s) URLs can be opened.")
    try:
        limit = max(500, min(int(max_chars), MAX_SNIPPET_CHARS))
    except (TypeError, ValueError):
        limit = DEFAULT_SNIPPET_CHARS
    page = _page(conv)
    try:
        page.goto(clean, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        raise PCError(f"Navigation failed: {exc}") from None
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    result = {"url": page.url, "title": page.title(), "text": _visible_text(page, limit)}
    _log(conv, "browser_open", url=page.url)
    return result


def browser_click(conv: Any, target: str) -> dict[str, Any]:
    clean = (target or "").strip()
    if not clean:
        raise PCError("Empty click target; use a CSS selector, 'text=...' or '#' + element index from browser_snapshot.")
    page = _page(conv)
    try:
        page.locator(clean).first.click(timeout=8000)
    except Exception as exc:
        raise PCError(f"Click failed on '{clean}': {exc}") from None
    result = {"url": page.url, "title": page.title()}
    _log(conv, "browser_click", target=clean)
    return result


def browser_type(conv: Any, target: str, text: str, press_enter: bool = False) -> dict[str, Any]:
    clean = (target or "").strip()
    if not clean:
        raise PCError("Empty input target; use a CSS selector, 'text=...' or '#' + element index from browser_snapshot.")
    page = _page(conv)
    try:
        field = page.locator(clean).first
        field.fill("")
        field.type((text or "").strip(), timeout=8000)
        if press_enter:
            field.press("Enter")
    except Exception as exc:
        raise PCError(f"Typing failed on '{clean}': {exc}") from None
    result = {"url": page.url, "typed": len(text or ""), "pressed_enter": bool(press_enter)}
    _log(conv, "browser_type", target=clean, chars=len(text or ""))
    return result


def browser_snapshot(conv: Any, max_chars: int = DEFAULT_SNIPPET_CHARS) -> dict[str, Any]:
    try:
        limit = max(500, min(int(max_chars), MAX_SNIPPET_CHARS))
    except (TypeError, ValueError):
        limit = DEFAULT_SNIPPET_CHARS
    page = _page(conv)
    links = page.eval_on_selector_all(
        "a", "els => els.slice(0, 40).map((e, i) => ({n: i, text: (e.textContent || '').trim().slice(0, 80), href: e.href}))"
    )
    inputs = page.eval_on_selector_all(
        "input, textarea, select",
        "els => els.slice(0, 20).map((e, i) => ({n: i, tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', placeholder: e.placeholder || ''}))",
    )
    buttons = page.eval_on_selector_all(
        "button, input[type=submit]",
        "els => els.slice(0, 20).map((e, i) => ({n: i, text: (e.textContent || e.value || '').trim().slice(0, 80)}))",
    )
    result = {
        "url": page.url,
        "title": page.title(),
        "text": _visible_text(page, limit),
        "links": links,
        "inputs": inputs,
        "buttons": buttons,
    }
    _log(conv, "browser_snapshot", url=page.url)
    return result


def browser_screenshot(conv: Any, path: str = "") -> dict[str, Any]:
    target = smart_path(path or "Desktop/actualone_screenshot.png")
    if target.suffix.lower() != ".png":
        target = target.with_suffix(".png")
    target.parent.mkdir(parents=True, exist_ok=True)
    page = _page(conv)
    try:
        page.screenshot(path=str(target), full_page=False)
    except Exception as exc:
        raise PCError(f"Screenshot failed: {exc}") from None
    result = {"path": str(target.resolve()), "url": page.url}
    _log(conv, "browser_screenshot", path=str(target))
    return result


def browser_close(conv: Any) -> dict[str, Any]:
    _stop()
    return {"closed": True}


BROWSER_IMPLS.update(
    {
        "browser_open": browser_open,
        "browser_click": browser_click,
        "browser_type": browser_type,
        "browser_snapshot": browser_snapshot,
        "browser_screenshot": browser_screenshot,
        "browser_close": browser_close,
    }
)


BROWSER_TOOL_SCHEMAS = [
    {
        "name": "browser_open",
        "description": "Open a URL in the controlled Chromium browser and return the page title and visible text. Use this instead of web_read when the page needs interaction.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full http(s) URL to open."},
                "max_chars": {"type": "integer", "description": "Maximum characters of page text to return (default 6000)."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": (
            "Click an element in the controlled browser. target is a Playwright selector:"
            " CSS ('#id', '.class'), XPath ('//button[2]') or 'text=Войти'. Inspect with"
            " browser_snapshot first."
        ),
        "parameters": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "Playwright selector of the element to click."}},
            "required": ["target"],
        },
    },
    {
        "name": "browser_type",
        "description": "Clear an input field in the controlled browser, type text into it and optionally press Enter (for search boxes and login forms).",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Playwright selector of the input field."},
                "text": {"type": "string", "description": "Text to type."},
                "press_enter": {"type": "boolean", "description": "Press Enter after typing."},
            },
            "required": ["target", "text"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "Inspect the current page: title, url, visible text, and lists of links, inputs and buttons with selectors for browser_click / browser_type.",
        "parameters": {
            "type": "object",
            "properties": {"max_chars": {"type": "integer", "description": "Maximum characters of page text (default 6000)."}},
        },
    },
    {
        "name": "browser_screenshot",
        "description": "Save a PNG screenshot of the current page. Default path: Desktop/actualone_screenshot.png.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Where to save the PNG; 'Desktop/<name>' is supported."}},
        },
    },
    {
        "name": "browser_close",
        "description": "Close the controlled browser.",
        "parameters": {"type": "object", "properties": {}},
    },
]

BROWSER_SECTION = """

Interactive browser tools (user-enabled, audit-logged in pc_log.jsonl): browser_open,
browser_snapshot, browser_click, browser_type, browser_screenshot, browser_close.
Flow for any site task: browser_open -> browser_snapshot (find selectors) -> browser_click /
browser_type (selectors like 'text=Sign in', '#login', '//input[@name=\"q\"]') -> browser_open
the result -> browser_screenshot when the user may want proof. If Playwright is missing the
tool returns an install hint - pass it to the user verbatim. Logins: type credentials only
when the user provided them in this conversation; never send them anywhere else.
Logins persist: the browser uses a saved profile (browser_profile folder next to the memory
dir, override with ANALYST_BROWSER_PROFILE), so a site that was logged in once stays logged
in across restarts. If a login check/captcha appears, ask the user to log in manually with
ANALYST_BROWSER_HEADED=1 once - after that the session is saved.
"""
