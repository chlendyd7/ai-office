"""Append-only run record, stored in SQLite (`events`). Single source of truth for Ops and Office."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .db import Database, open_db

log = logging.getLogger("aiops.store")
_ID = re.compile(r"[A-Za-z0-9_-]+")


class RunStore:
    def __init__(self, root: "str | Path | Database"):
        self.db = open_db(root)
        self._listeners: list[Callable[[str, dict], None]] = []

    def subscribe(self, fn: Callable[[str, dict], None]) -> None:
        """fn(run_id, event) is called after each append. A failing listener never affects the run."""
        self._listeners.append(fn)

    def new_run_id(self) -> str:
        return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"

    @staticmethod
    def _check(run_id: str) -> str:
        if not _ID.fullmatch(run_id):
            raise ValueError(f"invalid run id: {run_id!r}")
        return run_id

    def append(self, run_id: str, type_: str, /, **data) -> dict:
        self._check(run_id)
        with self.db.lock:
            seq = self.db.conn.execute("SELECT COALESCE(MAX(seq) + 1, 0) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0]
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self.db.conn.execute("INSERT INTO events (run_id, seq, ts, type, data) VALUES (?, ?, ?, ?, ?)",
                                 (run_id, seq, ts, type_, json.dumps(data, ensure_ascii=False)))
        ev = {"seq": seq, "ts": ts, "type": type_, **data}
        for fn in self._listeners:
            try:
                fn(run_id, ev)
            except Exception:
                log.exception("run listener failed")
        return ev

    def read(self, run_id: str) -> list[dict]:
        self._check(run_id)
        with self.db.lock:
            rows = self.db.conn.execute("SELECT seq, ts, type, data FROM events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        if not rows:
            raise KeyError(run_id)
        return [{"seq": s, "ts": ts, "type": t, **json.loads(d)} for s, ts, t, d in rows]

    def runs_with(self, key: str, value: str) -> list[str]:
        """Runs whose `run_started` carries `key` == `value` (newest first). Only the keys that have an index."""
        if key not in ("project_id", "project_run"):
            raise ValueError(f"no index for {key!r}")
        with self.db.lock:
            # run ids carry the start time to the second only: two runs started in one second are told apart by the event's own millisecond time
            rows = self.db.conn.execute(f"SELECT run_id FROM events WHERE type = 'run_started' AND json_extract(data, '$.{key}') = ? ORDER BY ts DESC, run_id DESC", (value,))
            return [r[0] for r in rows]

    def list_runs(self) -> list[str]:
        with self.db.lock:
            return [r[0] for r in self.db.conn.execute("SELECT DISTINCT run_id FROM events ORDER BY run_id")]
