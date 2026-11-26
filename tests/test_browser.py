"""Tests of the interactive browser tools (standalone/browser.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from standalone import agent as agent_mod, browser, memory
from standalone.browser import _visible_text
from standalone.pc import PCError


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))
    monkeypatch.delenv("ANALYST_PC_TOOLS", raising=False)
    monkeypatch.delenv("ANALYST_BROWSER_HEADED", raising=False)
    browser._state.clear()
    yield
    browser._state.clear()


@pytest.fixture()
def conv(tmp_path) -> memory.Conversation:
    return memory.Conversation("browse", tmp_path).load()


def fake_page(monkeypatch):
    page = MagicMock()
    page.url = "https://example.com/page?q=1"
    page.title.return_value = "Example"
    page.evaluate.return_value = "Hello world body text"
    page.eval_on_selector_all.side_effect = lambda sel, _: (
        [{"n": 0, "text": "Docs", "href": "https://example.com/docs"}] if sel == "a" else []
    )
    browser._state["page"] = page
    return page


def test_open_rejects_non_http(conv):
    with pytest.raises(PCError):
        browser.browser_open(conv, "file:///C:/x")


def test_open_flow_and_audit(conv, monkeypatch):
    fake_page(monkeypatch)
    result = browser.browser_open(conv, "https://example.com/page?q=1", max_chars=100)
    assert result["title"] == "Example" and "Hello world" in result["text"]
    log = (conv.root / "pc_log.jsonl").read_text(encoding="utf-8")
    assert "browser_open" in log


def test_click_and_type(conv, monkeypatch):
    page = fake_page(monkeypatch)
    browser.browser_click(conv, "text=Войти")
    page.locator.assert_called_with("text=Войти")
    browser.browser_type(conv, "#login", "user", press_enter=True)
    locator = page.locator.return_value.first
    assert locator.press.call_args[0] == ("Enter",)


def test_click_empty_target(conv, monkeypatch):
    fake_page(monkeypatch)
    with pytest.raises(PCError):
        browser.browser_click(conv, "  ")


def test_screenshot_path_desktop(conv, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    page = fake_page(monkeypatch)
    payload = json.loads(
        agent_mod.execute_tool(
            conv,
            {"name": "browser_screenshot", "arguments": {"path": "рабочий стол/shot.png"}},
        )
    )
    assert "error" not in payload
    assert payload["path"] == str((home / "Desktop" / "shot.png").resolve())
    page.screenshot.assert_called_once()


def test_visible_text_handles_failure():
    page = MagicMock()
    page.evaluate.side_effect = RuntimeError("no doc")
    assert _visible_text(page, 10) == ""


def test_browser_gated_without_pc(conv, monkeypatch):
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "browser_open", "arguments": {"url": "https://example.com"}})
    )
    assert "error" in payload


def test_browser_missing_playwright_hint(conv, monkeypatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    import builtins

    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "browser_open", "arguments": {"url": "https://example.com"}})
    )
    assert "error" in payload and "pip install playwright" in payload["error"]


def test_browser_tools_registered_when_enabled(monkeypatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    instructions, schemas = agent_mod._system_state()
    names = [s["name"] for s in schemas]
    for tool in browser.BROWSER_IMPLS:
        assert tool in names
    assert "browser_snapshot (find selectors)" in instructions


def test_profile_dir_env_override(monkeypatch, tmp_path):
    target = tmp_path / "prof"
    monkeypatch.setenv("ANALYST_BROWSER_PROFILE", str(target))
    assert browser._profile_dir(None) == target


def test_profile_dir_default_next_to_memory(tmp_path):
    conv = memory.Conversation("p", tmp_path / "mem").load()
    result = browser._profile_dir(conv)
    assert result == tmp_path / "browser_profile"
