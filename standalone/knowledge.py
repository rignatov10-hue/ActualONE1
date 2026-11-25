"""Local knowledge base for the agent: keyword RAG over the ``knowledge/`` folder.

Same app-dir convention as memory: ``<app_dir>/knowledge`` (overridable with
``ANALYST_KNOWLEDGE_DIR``). The agent indexes every text file there (Markdown,
TXT, CSV, JSON, source code, HTML) in chunks and answers ``search_knowledge``
queries with the most relevant excerpts. Pure stdlib: token-overlap scoring,
no external dependencies, rebuilt automatically when files change.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .pc import PCError, _audit

CHUNK_CHARS = 1600
MAX_FILE_BYTES = 2_000_000
MAX_RESULTS = 10
DEFAULT_RESULTS = 6
EXCERPT_CHARS = 420
TEXT_EXTS = {
    ".md", ".txt", ".log", ".csv", ".json", ".xml", ".html", ".yaml", ".yml",
    ".py", ".js", ".ts", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java",
    ".sh", ".bat", ".ps1", ".sql", ".ini", ".toml", ".conf",
}
_WORD = re.compile(r"[a-zа-яё0-9]+")
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}

_cache: dict[str, Any] = {"dir": None, "signature": None, "chunks": []}


def knowledge_dir(conv: Any) -> Path:
    override = os.environ.get("ANALYST_KNOWLEDGE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(conv.root).parent / "knowledge"


def _dir_signature(directory: Path) -> tuple:
    entries = []
    for path in directory.rglob("*"):
        if path.is_file() and not _SKIP_DIRS & set(path.parts):
            try:
                stat = path.stat()
                entries.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
    return tuple(sorted(entries))


def _tokenize(text: str) -> Counter:
    return Counter(match.group(0) for match in _WORD.finditer(text.lower()))


def _chunk_file(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix.lower() not in TEXT_EXTS or path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    chunks = []
    line = 1
    for offset in range(0, len(text), CHUNK_CHARS):
        piece = text[offset : offset + CHUNK_CHARS]
        start_line = line
        line += piece.count("\n")
        chunks.append(
            {
                "file": str(path),
                "line": start_line,
                "text": piece,
                "counter": _tokenize(piece),
            }
        )
    return chunks


def build_index(conv: Any) -> list[dict[str, Any]]:
    directory = knowledge_dir(conv)
    if not directory.is_dir():
        _cache.update({"dir": directory, "signature": (), "chunks": []})
        return []
    signature = _dir_signature(directory)
    if _cache.get("dir") == directory and _cache.get("signature") == signature:
        return _cache["chunks"]
    chunks: list[dict[str, Any]] = []
    for path in directory.rglob("*"):
        if _SKIP_DIRS & set(path.parts):
            continue
        chunks.extend(_chunk_file(path))
    _cache.update({"dir": directory, "signature": signature, "chunks": chunks})
    return chunks


def search_knowledge(conv: Any, query: str, max_results: int = DEFAULT_RESULTS) -> dict[str, Any]:
    terms = [t for t in (match.group(0) for match in _WORD.finditer((query or "").lower())) if len(t) > 1]
    if not terms:
        raise PCError("Empty query.")
    try:
        limit = max(1, min(int(max_results), MAX_RESULTS))
    except (TypeError, ValueError):
        limit = DEFAULT_RESULTS
    chunks = build_index(conv)
    if not chunks:
        directory = knowledge_dir(conv)
        raise PCError(
            f"The knowledge base is empty. Put .md/.txt/.csv files into {directory}"
            " and ask again (files are indexed automatically)."
        )
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk in chunks:
        score = 0.0
        for term in set(terms):
            matches = chunk["counter"].get(term, 0)
            if matches:
                score += 1.0 + 0.1 * min(matches, 10)
        if score:
            scored.append((score / (len(chunk["text"]) ** 0.5), chunk))
    scored.sort(key=lambda item: -item[0])
    results = [
        {
            "file": chunk["file"].replace(str(knowledge_dir(conv).resolve()), ""),
            "line": chunk["line"],
            "excerpt": chunk["text"].strip()[:EXCERPT_CHARS],
            "score": round(score, 4),
        }
        for score, chunk in scored[:limit]
    ]
    _audit(conv, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "search_knowledge", "query": query})
    return {"query": query, "files_indexed": len({c["file"] for c in chunks}), "matches": len(results), "results": results}


KNOWLEDGE_TOOL_SCHEMA = {
    "name": "search_knowledge",
    "description": (
        "Search the user's local knowledge base (the knowledge folder next to the"
        " memory directory) for passages relevant to a question. Use it when the"
        " answer may live in the user's own documents, notes or specs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to search for (2-6 key words works best)."},
            "max_results": {"type": "integer", "description": "Excerpts to return (default 6, max 10)."},
        },
        "required": ["query"],
    },
}

KNOWLEDGE_SECTION = """

Knowledge base: the search_knowledge tool finds passages in the user's own
documents (knowledge folder, indexed automatically). When a question touches
their files, projects, notes or stored specs, search there FIRST and answer
with citations like (file, line). Say the source file for every fact you take
from the knowledge base.
"""
