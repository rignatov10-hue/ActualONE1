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
