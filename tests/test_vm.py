"""Tests of the VirtualBox VM control tools (standalone/vm.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from standalone import agent as agent_mod
from standalone import memory, vm
from standalone.vm import _parse_vms, _resolve_name
from standalone.pc import PCError


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))
    monkeypatch.delenv("ANALYST_PC_TOOLS", raising=False)
    monkeypatch.delenv("ANALYST_VBOXMANAGE", raising=False)


@pytest.fixture()
def conv(tmp_path) -> memory.Conversation:
    return memory.Conversation("vms", tmp_path).load()


FAKE_LIST = '"kali-2026.3" {11111111-2222-3333-4444-555555555555}\n"win11" {66666666-7777-8888-9999-000000000000}\n'


def fake_vbox(monkeypatch, outputs, tmp_path=None):
    calls = []

    def fake_run(exe, args, timeout):
        calls.append((exe, args, timeout))
        key = " ".join(args[:2])
        out = outputs.get(key, "")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    fake = tmp_path / "VBoxManage.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr("standalone.vm.subprocess_run", fake_run)
    monkeypatch.setenv("ANALYST_VBOXMANAGE", str(fake))
    return calls


def test_parse_vms():
    entries = _parse_vms(FAKE_LIST)
    assert [e["name"] for e in entries] == ["kali-2026.3", "win11"]
    assert entries[0]["uuid"] == "11111111-2222-3333-4444-555555555555"


def test_resolve_name_strips_quotes():
    assert _resolve_name(' "kali" ') == "kali"
    with pytest.raises(PCError):
        _resolve_name("  ")


def test_vm_list_marks_running(monkeypatch, conv, tmp_path):
    calls = fake_vbox(
        monkeypatch,
        {"list vms": FAKE_LIST, "list runningvms": '"kali-2026.3" {11111111-2222-3333-4444-555555555555}\n'},
        tmp_path,
    )
    result = vm.vm_list(conv)
    names = {e["name"]: e["running"] for e in result["vms"]}
    assert names == {"kali-2026.3": True, "win11": False}
    assert calls[0][1] == ["list", "vms"]


def test_vm_start_and_save(monkeypatch, conv, tmp_path):
    calls = fake_vbox(monkeypatch, {"startvm kal": "VM 'kali' has been successfully started."}, tmp_path)
    result = vm.vm_start(conv, " kali ", headless=True)
    assert result["started"] is True and result["type"] == "headless"
    assert calls[0][1] == ["startvm", "kali", "--type", "headless"]
    vm.vm_save(conv, "kali")
    assert calls[1][1] == ["controlvm", "kali", "savestate"]
    log = (conv.root / "pc_log.jsonl").read_text(encoding="utf-8")
    assert "vm_start" in log and "vm_save" in log


def test_vm_run_invocation(monkeypatch, conv, tmp_path):
    calls = fake_vbox(monkeypatch, {}, tmp_path)
    vm.vm_run(conv, "kali", "dir", username="roman", password="pw")
    args = calls[0][1]
    assert args[0] == "guestcontrol" and "kali" in args
    assert "--username" in args and "roman" in args
    assert "--exe" in args


def test_vm_tools_gated_when_pc_disabled(conv):
    payload = json.loads(agent_mod.execute_tool(conv, {"name": "vm_list", "arguments": {}}))
    assert "error" in payload and "Управление ПК" in payload["error"]


def test_vm_tools_registered_when_enabled(monkeypatch):
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    instructions, schemas = agent_mod._system_state()
    names = [s["name"] for s in schemas]
    for tool in vm.VM_IMPLS:
        assert tool in names
    assert "vm_save to stop safely" in instructions
