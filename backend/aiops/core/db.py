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

-- editable library -----------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS datasets (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    workflow   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (          -- one input for a workflow; free to edit and delete (runs keep their own copy)
    id         TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    data       TEXT NOT NULL,               -- JSON: the workflow input
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_by_dataset ON items (dataset_id);

CREATE TABLE IF NOT EXISTS resources (      -- the user's own material (resume, experience notes): text only, correctable
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',   -- original file name, '' when pasted
    source_type TEXT NOT NULL,              -- txt | md | pdf | paste
    pages       INTEGER,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompts (        -- every save is a new immutable version
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    workflow   TEXT NOT NULL,
    role       TEXT NOT NULL,
    body       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS prompts_no_update BEFORE UPDATE ON prompts
BEGIN SELECT RAISE(ABORT, 'a prompt version is immutable: save a new version instead'); END;
CREATE TRIGGER IF NOT EXISTS prompts_no_delete BEFORE DELETE ON prompts
BEGIN SELECT RAISE(ABORT, 'a prompt version is immutable'); END;

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
