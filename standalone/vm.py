"""VirtualBox VM control for the standalone agent (Kali lab and similar).

Wraps the VBoxManage CLI - no extra Python dependencies. Gated by the same
ANALYST_PC_TOOLS switch as other PC tools; every action is audit-logged.
Override the binary path with ANALYST_VBOXMANAGE.

vm_start / vm_save / vm_poweroff manage the VM itself; vm_run executes a
command INSIDE the guest via Guest Additions (needs a guest account).
Powering off is destructive - the agent must call request_approval first
(the section text enforces this).
"""

from __future__ import annotations

import os
import re
import shutil
import time
from typing import Any

from .pc import PCError, _audit

VM_IMPLS: dict[str, Any] = {}
VM_TOOLS = ("vm_list", "vm_start", "vm_save", "vm_poweroff", "vm_status", "vm_run")


def _vboxmanage() -> str:
    override = os.environ.get("ANALYST_VBOXMANAGE", "").strip()
    if override:
        if not os.path.isfile(override):
            raise PCError(f"ANALYST_VBOXMANAGE points to a missing file: {override}")
        return override
    found = shutil.which("VBoxManage")
    if found:
        return found
    candidates = (
        r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
        r"C:\Program Files (x86)\Oracle\VirtualBox\VBoxManage.exe",
        "/usr/bin/VBoxManage",
        "/usr/local/bin/VBoxManage",
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise PCError(
        "VBoxManage not found; install VirtualBox or set the env variable"
        " ANALYST_VBOXMANAGE to the full path of VBoxManage.exe."
    )


def _run_vm(args: list[str], timeout: int = 90) -> str:
    try:
        completed = subprocess_run(_vboxmanage(), args, timeout)
    except OSError as exc:
        raise PCError(f"Could not run VBoxManage: {exc}") from None
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()[:400]
        raise PCError(f"VBoxManage failed: {stderr}")
    return completed.stdout or ""


def subprocess_run(exe: str, args: list[str], timeout: int) -> Any:
    import subprocess

    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)


def _parse_vms(out: str) -> list[dict[str, Any]]:
    vms = []
    for line in out.splitlines():
        match = re.match(r'^"(.+?)"\s+\{(.+?)\}$', line.strip())
        if match:
            vms.append({"name": match.group(1), "uuid": match.group(2), "running": False})
    return vms


def _log(conv: Any, tool: str, **extra: Any) -> None:
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, **extra})


def _running_set() -> set[str]:
    out = _run_vm(["list", "runningvms"])
    return {entry["name"] for entry in _parse_vms(out)}


def vm_list(conv: Any) -> dict[str, Any]:
    vms = _parse_vms(_run_vm(["list", "vms"]))
    running = _running_set()
    for entry in vms:
        entry["running"] = entry["name"] in running
    _log(conv, "vm_list", count=len(vms))
    return {"vms": vms}


def _resolve_name(name: str) -> str:
    clean = (name or "").strip().strip('"')
    if not clean:
        raise PCError("Empty VM name; call vm_list to see the available machines.")
    return clean


def vm_start(conv: Any, name: str, headless: bool = True) -> dict[str, Any]:
    name = _resolve_name(name)
    started = _run_vm(["startvm", name, "--type", "headless" if headless else "gui"])
    _log(conv, "vm_start", name=name, headless=bool(headless))
    return {"name": name, "started": True, "type": "headless" if headless else "gui", "output": started.strip()[:300]}


def vm_save(conv: Any, name: str) -> dict[str, Any]:
    name = _resolve_name(name)
    _run_vm(["controlvm", name, "savestate"])
    _log(conv, "vm_save", name=name)
    return {"name": name, "saved": True}


def vm_poweroff(conv: Any, name: str) -> dict[str, Any]:
    name = _resolve_name(name)
    _run_vm(["controlvm", name, "poweroff"])
    _log(conv, "vm_poweroff", name=name)
    return {"name": name, "powered_off": True}


def vm_status(conv: Any, name: str = "") -> dict[str, Any]:
    name = _resolve_name(name)
    info = _run_vm(["showvminfo", name, "--machinereadable"])
    fields: dict[str, str] = {}
    for line in info.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')
    result = {
        "name": name,
        "state": fields.get("VMState", "unknown"),
        "memory_mb": fields.get("memory", "?"),
        "cpus": fields.get("cpus", "?"),
        "os_type": fields.get("ostype", "?"),
    }
    _log(conv, "vm_status", name=name)
    return result


def vm_run(
    conv: Any,
    name: str,
    command: str,
    username: str = "",
    password: str = "",
    exe: str = "",
) -> dict[str, Any]:
    name = _resolve_name(name)
    clean = (command or "").strip()
    if not clean:
        raise PCError("Empty command.")
    guest_exe = (exe or "").strip() or "C:\\Windows\\System32\\cmd.exe"
    args = ["guestcontrol", name, "run", "--wait-stdout", "--wait-stderr"]
    if username:
        args += ["--username", username]
        if password:
            args += ["--password", password]
    args += ["--exe", guest_exe, "--", guest_exe]
    if guest_exe.lower().endswith("cmd.exe"):
        args += ["/c", *clean.split()]
    else:
        args += ["-lc", clean] if guest_exe.endswith("sh") else clean.split()
    try:
        stdout = _run_vm(args, timeout=120)
    except PCError as exc:
        raise PCError(
            f"{exc} (vm_run needs VirtualBox Guest Additions inside the VM and its credentials."
            " Linux guests: use the exe '/usr/bin/bash' with args like '-lc \"ls\"'.)"
        ) from None
    result = {"name": name, "command": clean, "stdout": stdout.strip()[:4000]}
    _log(conv, "vm_run", name=name, command=clean)
    return result


VM_IMPLS.update(
    {
        "vm_list": vm_list,
        "vm_start": vm_start,
        "vm_save": vm_save,
        "vm_poweroff": vm_poweroff,
        "vm_status": vm_status,
        "vm_run": vm_run,
    }
)

VM_SECTION = """

VirtualBox VM tools (user-enabled, audit-logged in pc_log.jsonl): vm_list, vm_start,
vm_save, vm_poweroff, vm_status, vm_run.
Flow: vm_list to see machines -> vm_start (headless by default; headless=false shows
the window) -> vm_status -> vm_run to execute commands inside the guest (needs Guest
Additions and the guest account; Windows guests run via cmd.exe, Linux guests pass
exe '/usr/bin/bash' with command '-lc "<shell line>"') -> vm_save to stop safely.
vm_poweroff loses unsaved guest data - always call request_approval before it.
When the user asks for Kali/OSINT work, start their Kali VM and work inside it.
"""


VM_TOOL_SCHEMAS = [
    {
        "name": "vm_list",
        "description": "List all VirtualBox VMs with their UUIDs and whether they are running.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "vm_start",
        "description": "Start a VirtualBox VM. Default headless (background); headless=false shows the window.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "VM name or UUID (see vm_list)."},
                "headless": {"type": "boolean", "description": "Run in background (default true)."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "vm_save",
        "description": "Save the VM state and stop it (safe pause-on-disk, resumable).",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "VM name or UUID."}},
            "required": ["name"],
        },
    },
    {
        "name": "vm_poweroff",
        "description": "Hard power-off of a VM (unsaved guest data is lost). Destructive: request_approval first.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "VM name or UUID."}},
            "required": ["name"],
        },
    },
    {
        "name": "vm_status",
        "description": "VM state: running/powered off/saved, memory, CPUs, guest OS type.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "VM name or UUID."}},
            "required": ["name"],
        },
    },
    {
        "name": "vm_run",
        "description": (
            "Run a command INSIDE the running VM (VirtualBox Guest Additions required)."
            " Windows guests: plain commands run via cmd.exe; Linux guests set"
            " exe to /usr/bin/bash and command to -lc '<shell line>'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "VM name or UUID."},
                "command": {"type": "string", "description": "Command line to run in the guest."},
                "exe": {"type": "string", "description": "Guest executable path inside the VM; default cmd.exe for Windows guests."},
                "username": {"type": "string", "description": "Guest account login."},
                "password": {"type": "string", "description": "Guest account password."},
            },
            "required": ["name", "command", "username"],
        },
    },
]
