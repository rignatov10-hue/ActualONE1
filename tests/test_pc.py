"""Tests of the opt-in local PC control tools (standalone/pc.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from standalone import agent as agent_mod
from standalone import memory, pc
from standalone.pc import PCError


@pytest.fixture(autouse=True)
def isolated_skills(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))
    monkeypatch.delenv("ANALYST_PC_TOOLS", raising=False)


@pytest.fixture()
def conv(tmp_path: Path) -> memory.Conversation:
    return memory.Conversation("pcs", tmp_path).load()


def test_disabled_by_default():
    assert not pc.enabled()
    assert "run_shell" not in agent_mod.INSTRUCTIONS
    instructions, schemas = agent_mod._system_state()
    assert all(schema["name"] not in pc.PC_IMPLS for schema in schemas)
    assert "pc_log.jsonl" not in instructions


def test_enabled_env_extends_tools(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    instructions, schemas = agent_mod._system_state()
    names = [schema["name"] for schema in schemas]
    for tool in pc.PC_IMPLS:
        assert tool in names
    assert "pc_log.jsonl" in instructions
    monkeypatch.setenv("ANALYST_PC_TOOLS", "off")
    instructions, schemas = agent_mod._system_state()
    assert all(schema["name"] not in pc.PC_IMPLS for schema in schemas)


def test_disabled_execute_tool_is_rejected(conv: memory.Conversation):
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "run_shell", "arguments": {"command": "echo hi"}})
    )
    assert "error" in payload
    assert not (conv.root / pc.AUDIT_NAME).exists()


def test_run_shell_executes_and_audits(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "run_shell", "arguments": {"command": "echo hello-pc"}})
    )
    assert payload["returncode"] == 0
    assert "hello-pc" in payload["stdout"]
    log = (conv.root / pc.AUDIT_NAME).read_text(encoding="utf-8")
    assert "run_shell" in log and "echo hello-pc" in log


def test_run_shell_timeout(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    call = {"name": "run_shell", "arguments": {"command": "sleep 5", "timeout": 1}}
    payload = json.loads(agent_mod.execute_tool(conv, call))
    assert "error" in payload
    assert "killed" in payload["error"]


def test_list_read_write_file_roundtrip(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    target = tmp_path / "deep" / "note.txt"
    written = json.loads(
        agent_mod.execute_tool(
            conv, {"name": "write_file", "arguments": {"path": str(target), "content": "толстые данные"}}
        )
    )
    assert written["written_chars"] > 0
    listed = json.loads(
        agent_mod.execute_tool(conv, {"name": "list_dir", "arguments": {"path": str(tmp_path / "deep")}})
    )
    assert listed["entries"][0]["name"] == "note.txt"
    read = json.loads(
        agent_mod.execute_tool(conv, {"name": "read_file", "arguments": {"path": str(target)}})
    )
    assert "толстые данные" in read["content"]


def test_file_tool_errors(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    missing = json.loads(
        agent_mod.execute_tool(conv, {"name": "read_file", "arguments": {"path": "/no/such/file"}})
    )
    assert "error" in missing
    noturl = json.loads(
        agent_mod.execute_tool(conv, {"name": "open_url", "arguments": {"url": "ftp://x"}})
    )
    assert "error" in noturl
    bad = json.loads(agent_mod.execute_tool(conv, {"name": "list_dir", "arguments": {"wat": 1}}))
    assert "error" in bad
    assert "Bad arguments" in bad["error"]


def test_open_path_uses_launcher(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    opened: list[str] = []
    monkeypatch.setattr(pc, "_launch", lambda target: opened.append(target))
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "open_path", "arguments": {"path": str(tmp_path)}})
    )
    assert payload["opened"] == str(tmp_path.resolve())
    assert opened


def test_write_file_size_limit(conv: memory.Conversation, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    payload = json.loads(
        agent_mod.execute_tool(
            conv,
            {"name": "write_file", "arguments": {"path": "x.txt", "content": "a" * (pc.MAX_WRITE_CHARS + 1)}},
        )
    )
    assert "error" in payload


def test_pc_error_class():
    with pytest.raises(PCError):
        raise PCError("test")
