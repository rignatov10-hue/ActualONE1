"""Persistent, provider-independent conversation memory on local disk.

Layout under the memory root (default ``./memory``):

- ``conversations/<name>.jsonl`` - one JSON message event per line, appended
  after every turn event, so a crash can lose at most the live turn;
- ``datasets/<name>.parquet`` + ``.meta.json`` (+ ``.scope.parquet``) - the
  loaded dataset and any narrowed scope, reloaded on start.

No AI provider stores or serves this memory: it is ordinary files you own.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from typing import Any

import pandas as pd

from .tools import Dataset, utc_now

MEMORY_ROOT = Path(os.environ.get("ANALYST_MEMORY_DIR", "memory"))
SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")


def safe_name(name: str) -> str:
    cleaned = SAFE_NAME.sub("_", name.strip())[:64]
    return cleaned or "default"


class Conversation:
    """One named conversation: its message history and its loaded dataset."""

    def __init__(self, name: str, root: Path | str | None = None) -> None:
        self.name = safe_name(name)
        self.root = Path(root or MEMORY_ROOT).resolve()
        self.messages: list[dict[str, Any]] = []
        self.dataset: Dataset | None = None
        self.full: pd.DataFrame | None = None
        self.active: pd.DataFrame | None = None
        self.active_description = "all rows of the loaded file"
        self.approval_hook = None
        self.activity_hook = None

    # ---- paths -------------------------------------------------------------
    @property
    def history_path(self) -> Path:
        return self.root / "conversations" / f"{self.name}.jsonl"

    @property
    def data_path(self) -> Path:
        return self.root / "datasets" / f"{self.name}.parquet"

    @property
    def meta_path(self) -> Path:
        return self.root / "datasets" / f"{self.name}.meta.json"

    @property
    def scope_path(self) -> Path:
        return self.root / "datasets" / f"{self.name}.scope.parquet"

    # ---- load / save -------------------------------------------------------
    def load(self) -> "Conversation":
        self.approval_hook = None
        self.activity_hook = None
        if self.history_path.exists():
            events: list[dict[str, Any]] = []
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            self.messages = events
        self._load_dataset()
        return self

    def _load_dataset(self) -> None:
        if not (self.data_path.exists() and self.meta_path.exists()):
            return
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            full = pd.read_parquet(self.data_path)
            dataset = Dataset(
                source=meta["source"], title=meta["title"], load_time=meta["load_time"]
            )
        except Exception:
            return
        scoped = bool(meta.get("scoped")) and self.scope_path.exists()
        active = pd.read_parquet(self.scope_path) if scoped else full
        self.dataset = dataset
        self.full = full
        self.active = active
        self.active_description = (
            f"the narrowed selection held over from earlier ({dataset.title})"
            if scoped
            else "all rows of the loaded file"
        )

    def append(self, event: dict[str, Any]) -> None:
        self.messages.append(event)
        self.save()

    def save(self) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("w", encoding="utf-8") as handle:
            for event in self.messages:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    # ---- context management -------------------------------------------------
    def model_messages(self) -> list[dict[str, Any]]:
        """History for the model: everything except summary events."""
        return [event for event in self.messages if event.get("role") != "summary"]

    def latest_summary(self) -> str:
        for event in reversed(self.messages):
            if event.get("role") == "summary":
                return str(event.get("content") or "")
        return ""

    def condense(self, summary_text: str, keep: int) -> None:
        """Compress the history: one summary event plus the last `keep` events."""
        summary_event = {"role": "summary", "content": summary_text, "time": utc_now()}
        self.messages = [summary_event] + self.model_messages()[-keep:]
        self.save()

    def forget(self) -> None:
        """Drop the whole history (dataset keeps its files)."""
        self.messages = []
        self.save()

    # ---- dataset state -----------------------------------------------------
    def set_dataset(self, dataset: Dataset, frame: pd.DataFrame) -> None:
        self.dataset = dataset
        self.full = frame
        self.active = frame
        self.active_description = "all rows of the loaded file"
        self._save_dataset()

    def set_scope(self, active: pd.DataFrame, description: str) -> None:
        self.active = active
        self.active_description = description
        self._save_dataset()

    def clear_scope(self) -> None:
        if self.full is None:
            return
        self.active = self.full
        self.active_description = "all rows of the loaded file"
        self._save_dataset()

    def _save_dataset(self) -> None:
        if self.dataset is None or self.full is None:
            return
        try:
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            self.full.to_parquet(self.data_path)
            scoped = self.active_description != "all rows of the loaded file"
            if scoped and self.active is not None:
                self.active.to_parquet(self.scope_path)
            elif self.scope_path.exists():
                self.scope_path.unlink()
            self.meta_path.write_text(
                json.dumps(
                    {
                        "source": self.dataset.source,
                        "title": self.dataset.title,
                        "load_time": self.dataset.load_time or utc_now(),
                        "full_rows": int(self.full.shape[0]),
                        "scoped": scoped,
                    },
                    ensure_ascii=False,
                )
            )
        except OSError:
            pass


def list_conversations(root: Path | str | None = None) -> list[str]:
    directory = Path(root or MEMORY_ROOT).resolve() / "conversations"
    if not directory.exists():
        return []
    return sorted(path.stem for path in directory.glob("*.jsonl"))


def conversation_stats(root: Path | str | None = None) -> dict[str, int]:
    """Saved dialog name -> number of user/assistant messages kept on disk."""
    directory = Path(root or MEMORY_ROOT).resolve() / "conversations"
    stats: dict[str, int] = {}
    if not directory.is_dir():
        return stats
    for path in sorted(directory.glob("*.jsonl")):
        count = 0
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = str(event.get("role") or "")
                    if role in {"user", "assistant"} and str(event.get("content") or "").strip():
                        count += 1
        except OSError:
            continue
        stats[path.stem] = count
    return stats


def search_conversations(
    root: Path | str | None, query: str, max_results: int = 30
) -> list[dict[str, str]]:
    """Search all saved dialogs for a phrase; hits as {dialog, role, excerpt}."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    directory = Path(root or MEMORY_ROOT).resolve() / "conversations"
    results: list[dict[str, str]] = []
    if not directory.is_dir():
        return results
    for path in sorted(directory.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            if len(results) >= max_results:
                return results
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(event.get("role") or "") not in {"user", "assistant"}:
                continue
            content = " ".join(str(event.get("content") or "").split())
            position = content.lower().find(needle)
            if position < 0:
                continue
            start = max(0, position - 60)
            excerpt = content[start : position + 120].strip()
            if start > 0:
                excerpt = "..." + excerpt
            if position + 120 < len(content):
                excerpt += "..."
            results.append({"dialog": path.stem, "role": str(event.get("role")), "excerpt": excerpt})
    return results


CONTINUATION_SUFFIX = ".cont"


def _continuation_path(conv: Any) -> Path:
    return Path(conv.history_path).with_suffix(CONTINUATION_SUFFIX)


def set_continuation_flag(conv: Any) -> None:
    """Remember that the last assistant reply of this dialog was cut off mid-text."""
    try:
        path = _continuation_path(conv)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1", encoding="utf-8")
    except OSError:
        pass


def clear_continuation_flag(conv: Any) -> bool:
    """Drop the continuation marker; True when it was present."""
    path = _continuation_path(conv)
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def has_continuation_flag(conv: Any) -> bool:
    try:
        return _continuation_path(conv).exists()
    except OSError:
        return False


def delete_conversation(root: Path | str | None, name: str) -> bool:
    """Remove a conversation's history and dataset files; True when anything was deleted."""
    root = Path(root or MEMORY_ROOT).resolve()
    clean = safe_name(name)
    removals = (
        root / "conversations" / f"{clean}.jsonl",
        root / "conversations" / f"{clean}.cont",
        root / "datasets" / f"{clean}.parquet",
        root / "datasets" / f"{clean}.meta.json",
        root / "datasets" / f"{clean}.scope.parquet",
    )
    removed = False
    for path in removals:
        if path.exists():
            try:
                path.unlink()
                removed = True
            except OSError:
                pass
    return removed

# ---- long-term facts (cross-conversation) ------------------------------------

FACTS_NAME = "facts.json"


def facts_path(root: Path | str) -> Path:
    return Path(root).resolve() / FACTS_NAME


def load_facts(root: Path | str) -> dict[str, str]:
    try:
        data = json.loads(facts_path(root).read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in data.items() if str(value).strip()}
    except (OSError, json.JSONDecodeError):
        return {}


def save_fact(root: Path | str, key: str, value: str) -> dict[str, str]:
    path = facts_path(root)
    current = load_facts(root)
    if value:
        current[key] = value
    else:
        current.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def facts_section(facts: dict[str, str]) -> str:
    if not facts:
        return ""
    listing = "\n".join(f"- {key}: {content}" for key, content in sorted(facts.items()))
    return (
        "\n\nLong-term facts about the user (kept in memory/facts.json across conversations):\n"
        + listing
        + "\nUse them quietly; update or extend them with save_fact when the user shares something durable."
    )
