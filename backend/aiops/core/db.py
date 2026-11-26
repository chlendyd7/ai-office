"""One SQLite file holds everything: the run record, the job queue, and the editable library (datasets, prompts).

The run record keeps its append-only guarantee at the database level: triggers refuse UPDATE and DELETE on `events`
(and on the queue log), so "nothing rewrites history" no longer depends on the code being careful.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    ts     TEXT NOT NULL,
    type   TEXT NOT NULL,
    data   TEXT NOT NULL,            -- JSON of the event's remaining fields
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS queue_events (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    data TEXT NOT NULL               -- JSON incl. the event type
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'the run record is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'the run record is append-only'); END;
CREATE TRIGGER IF NOT EXISTS queue_events_no_update BEFORE UPDATE ON queue_events
BEGIN SELECT RAISE(ABORT, 'the queue log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS queue_events_no_delete BEFORE DELETE ON queue_events
BEGIN SELECT RAISE(ABORT, 'the queue log is append-only'); END;
"""


class Database:
    """A single shared connection guarded by a lock (the app is one process: API threads + one worker thread)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)  # autocommit
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def open_db(root: "str | Path | Database") -> Database:
    """A directory means `<dir>/aiops.db`; a path ending in `.db` is the file itself."""
    if isinstance(root, Database):
        return root
    p = Path(root)
    return Database(p if p.suffix == ".db" else p / "aiops.db")
