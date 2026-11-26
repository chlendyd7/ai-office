"""Continuous operation: a persistent job queue plus a worker that keeps the agents busy without waiting for humans.

The worker never finalizes anything: a finished loop stops at `awaiting_approval` like any other run, and the worker
simply moves on to the next job. A human decides later (asynchronously). Guard rails keep it from running away:

  - budget       a fresh budget is given at every start; when the runs started in this session have spent it, pause
  - backpressure no new job while `max_pending` runs are waiting for a human decision
  - failures     pause after N consecutive failed runs (e.g. gateway down) instead of burning through the queue
  - restart      a restarted server always comes back paused

The cap is checked when a job is about to start; a running job still ends inside its own per-run budget.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import Database, open_db
from .engine import Engine
from .summary import derive_status, summarize

log = logging.getLogger("aiops.queue")
TERMINAL = {"approved", "rejected", "failed"}


class JobQueue:
    """Append-only log of queue events, folded into job states. Survives restarts."""

    def __init__(self, db: "str | Path | Database"):
        self.db = open_db(db)

    def _append(self, **ev) -> None:
        with self.db.lock:
            self.db.conn.execute("INSERT INTO queue_events (ts, data) VALUES (?, ?)",
                                 (datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(ev, ensure_ascii=False)))

    def jobs(self) -> list[dict]:
        jobs: dict[str, dict] = {}
        with self.db.lock:
            rows = self.db.conn.execute("SELECT ts, data FROM queue_events ORDER BY id").fetchall()
        for ts, data in rows:
            e = {"ts": ts, **json.loads(data)}
            j = jobs.get(e.get("job_id"))
            t = e["type"]
            if t == "enqueued":
                jobs[e["job_id"]] = {"job_id": e["job_id"], "workflow": e["workflow"], "input": e["input"], "overrides": e["overrides"],
                                     "status": "queued", "run_id": None, "error": None, "ts": e["ts"]}
            elif j and t == "started":
                j.update(status="started", run_id=e["run_id"])
            elif j and t == "cancelled" and j["status"] == "queued":
                j["status"] = "cancelled"
            elif j and t == "start_failed":
                j.update(status="start_failed", error=e["error"])
        return list(jobs.values())

    def enqueue(self, items: list[dict]) -> list[str]:
        ids = []
        for it in items:
            jid = uuid.uuid4().hex[:10]
            self._append(type="enqueued", job_id=jid, workflow=it["workflow"], input=it["input"], overrides=it.get("overrides") or {})
            ids.append(jid)
        return ids

    def mark_started(self, job_id: str, run_id: str) -> None:
        self._append(type="started", job_id=job_id, run_id=run_id)

    def start_failed(self, job_id: str, error: str) -> None:
        self._append(type="start_failed", job_id=job_id, error=error)

    def cancel(self, job_id: str) -> bool:
        if any(j["job_id"] == job_id and j["status"] == "queued" for j in self.jobs()):
            self._append(type="cancelled", job_id=job_id)
            return True
        return False

    def cancel_all(self) -> int:
        n = 0
        for j in self.jobs():
            if j["status"] == "queued":
                self._append(type="cancelled", job_id=j["job_id"])
                n += 1
        return n


DEFAULTS = {"budget_usd": 1.0, "max_pending": 3, "concurrency": 1, "max_failures": 3}


class Worker:
    def __init__(self, engine: Engine, queue: JobQueue, interval: float = 0.5, background: bool = True):
        self.engine, self.queue, self.interval, self.background = engine, queue, interval, background
        self.enabled = False
        self.configured = False  # has someone started it in this process? (gates a one-click resume elsewhere)
        self.reason = "paused"  # paused | idle | working | backpressure | budget | failures
        self.settings = dict(DEFAULTS)
        self._session: list[str] = []  # run ids started since the last start
        self._terminal: dict[str, str] = {}  # status cache: finished runs never change
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- control ----
    def start(self, **settings) -> None:
        with self._lock:
            s = {**self.settings, **{k: v for k, v in settings.items() if v is not None}}
            if not (s["budget_usd"] > 0 and s["max_pending"] >= 1 and 1 <= s["concurrency"] <= 4 and s["max_failures"] >= 1):
                raise ValueError("invalid settings: budget_usd>0, max_pending>=1, 1<=concurrency<=4")
            self.settings = s
            self._session = []  # fresh budget and failure streak at every start
            self.enabled = self.configured = True
            self.reason = "working"

    def resume(self) -> None:
        """Continue the SAME session after a manual pause (spent budget and failure streak are kept). A stop caused by the
        budget or by failures needs an explicit `start` with a new budget, so a one-click resume can't dodge the cap."""
        with self._lock:
            if not self.configured or self.reason != "paused":
                raise ValueError("can resume only after a manual pause; use start with a budget" if self.configured
                                 else "never started: use start with a budget")
            self.enabled = True
            self.reason = "working"

    def pause(self, reason: str = "paused") -> None:
        with self._lock:
            self.enabled = False
            self.reason = reason

    # ---- observation ----
    def _status_of(self, rid: str) -> str:
        if rid in self._terminal:
            return self._terminal[rid]
        s = derive_status(self.engine.store.read(rid))
        if s in TERMINAL:
            self._terminal[rid] = s
        return s

    def _snapshot(self) -> dict:
        store = self.engine.store
        pending = sum(1 for rid in store.list_runs() if self._status_of(rid) == "awaiting_approval")
        spent, running, statuses = 0.0, 0, []
        for rid in self._session:
            ev = store.read(rid)
            spent += sum(e["cost_usd"] for e in ev if e["type"] == "llm_call")
            st = derive_status(ev)
            running += st == "running"
            statuses.append(st)
        return {"pending": pending, "spent": spent, "running": running, "statuses": statuses}

    def status(self) -> dict:
        with self._lock:
            snap = self._snapshot()
            queued = sum(1 for j in self.queue.jobs() if j["status"] == "queued")
            return {"enabled": self.enabled, "configured": self.configured, "reason": self.reason, "settings": self.settings,
                    "spent_usd": round(snap["spent"], 6), "queued": queued, "running": snap["running"],
                    "pending_approvals": snap["pending"], "started": len(self._session)}

    # ---- the loop ----
    def tick(self) -> str:
        """One scheduling decision. Returns "started:<run_id>" or why nothing was started."""
        with self._lock:
            if not self.enabled:
                return "paused"
            snap, s = self._snapshot(), self.settings
            if snap["spent"] >= s["budget_usd"]:
                self.pause("budget")
                return "budget"
            last = snap["statuses"][-s["max_failures"]:]
            if len(last) >= s["max_failures"] and all(x == "failed" for x in last):
                self.pause("failures")
                return "failures"
            if snap["running"] >= s["concurrency"]:
                self.reason = "working"
                return "busy"
            if snap["pending"] >= s["max_pending"]:
                self.reason = "backpressure"
                return "backpressure"
            job = next((j for j in self.queue.jobs() if j["status"] == "queued"), None)
            if job is None:
                self.reason = "idle"
                return "idle"
            try:
                run_id = self.engine.create(job["workflow"], job["input"], job["overrides"])
            except Exception as e:  # a bad job must not stall the queue
                self.queue.start_failed(job["job_id"], f"{type(e).__name__}: {e}")
                return "start_failed"
            self.queue.mark_started(job["job_id"], run_id)
            self._session.append(run_id)
            self.reason = "working"
        if self.background:
            threading.Thread(target=self.engine.run_pass, args=(run_id,), daemon=True, name=f"run-{run_id}").start()
        else:
            self.engine.run_pass(run_id)
        return f"started:{run_id}"

    def run_forever(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                log.exception("worker tick failed")

    def start_thread(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, daemon=True, name="queue-worker")
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
