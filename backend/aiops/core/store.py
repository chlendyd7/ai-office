"""Append-only run record: one JSONL file per run. Single source of truth for Ops and Office."""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("aiops.store")
_ID = re.compile(r"[A-Za-z0-9_-]+")


class RunStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[str, dict], None]] = []

    def subscribe(self, fn: Callable[[str, dict], None]) -> None:
        """fn(run_id, event) is called after each append. A failing listener never affects the run."""
        self._listeners.append(fn)

    def new_run_id(self) -> str:
        return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"

    def _path(self, run_id: str) -> Path:
        if not _ID.fullmatch(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        return self.root / f"{run_id}.jsonl"

    def append(self, run_id: str, type_: str, /, **data) -> dict:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)  # survive the runs folder being cleaned while the server runs
            p = self._path(run_id)
            seq = 0
            if p.exists():
                with p.open(encoding="utf-8") as f:
                    seq = sum(1 for _ in f)
            ev = {
                "seq": seq,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "type": type_,
                **data,
            }
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        for fn in self._listeners:
            try:
                fn(run_id, ev)
            except Exception:
                log.exception("run listener failed")
        return ev

    def read(self, run_id: str) -> list[dict]:
        p = self._path(run_id)
        if not p.exists():
            raise KeyError(run_id)
        with self._lock:  # never observe a half-written line
            with p.open(encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]

    def list_runs(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.jsonl"))
