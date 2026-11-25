"""Static checks of gui.py that run without tkinter installed."""

from __future__ import annotations

import ast
from pathlib import Path

GUI = Path(__file__).resolve().parents[1] / "standalone" / "gui.py"

TKINTER_CONSTANTS = {
    "BOTH", "BOTTOM", "DISABLED", "END", "LEFT", "NORMAL", "RIGHT", "TOP",
    "W", "E", "N", "S", "X", "Y",
}


def import_names(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            imported |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
    return imported


def module_names(tree: ast.AST) -> set[str]:
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    return defined


def test_tkinter_constants_are_imported():
    tree = ast.parse(GUI.read_text(encoding="utf-8"))
    imported = import_names(tree)
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    missing = {name for name in used & TKINTER_CONSTANTS if name not in imported}
    assert not missing, f"tkinter constants used but not imported: {sorted(missing)}"


def test_no_bare_unknown_uppercase_names():
    tree = ast.parse(GUI.read_text(encoding="utf-8"))
    known = TKINTER_CONSTANTS | {"ttk", "tk", "BG", "PANEL", "FIELD", "LINE", "FG", "DIM", "AGENT_TXT"}
    defined = module_names(tree) | import_names(tree)
    suspicious = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id.isupper() and len(node.id) <= 8
        and node.id not in defined
        and not node.id.startswith("_")
        and node.id not in known
    }
    assert not suspicious, f"uppercase names used but never defined: {sorted(suspicious)}"
