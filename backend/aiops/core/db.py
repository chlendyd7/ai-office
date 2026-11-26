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
    updated_at  TEXT NOT NULL,
    review_status  TEXT NOT NULL DEFAULT 'verified',  -- 'unverified' until a human has checked an LLM conversion against the original
    converted_by   TEXT NOT NULL DEFAULT 'text',      -- text | llm | mixed
    convert_run_id TEXT NOT NULL DEFAULT ''           -- the `convert` run that holds the cost, '' when no model was used
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


# Columns added after a table first shipped: CREATE TABLE IF NOT EXISTS leaves an existing database untouched, so add them here.
ADDED_COLUMNS = {
    "resources": {
        "review_status": "TEXT NOT NULL DEFAULT 'verified'",
        "converted_by": "TEXT NOT NULL DEFAULT 'text'",
        "convert_run_id": "TEXT NOT NULL DEFAULT ''",
    },
}


# Numbered migrations after the v1 baseline (SCHEMA + ADDED_COLUMNS, which every database already has). Each is a list of
# statements run in ONE transaction together with the version bump: it either applies completely or not at all.
# v2: projects, deliverables and what they use (see docs/design-task-schema.md). No foreign keys on purpose: deletes are
# done by one function in one transaction and `integrity_problems` audits what is left.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        """CREATE TABLE projects (
            id         TEXT PRIMARY KEY,
            office     TEXT NOT NULL,
            title      TEXT NOT NULL,
            brief      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(brief)),
            notes      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX projects_by_office ON projects (office, updated_at)",
        """CREATE TABLE deliverables (
            id            TEXT PRIMARY KEY,
            project_id    TEXT NOT NULL,
            position      INTEGER NOT NULL,
            kind          TEXT NOT NULL,
            title         TEXT NOT NULL,
            spec          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(spec)),
            final_content TEXT,
            final_meta    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(final_meta)),
            final_run_id  TEXT NOT NULL DEFAULT '',
            final_at      TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )""",
        "CREATE INDEX deliverables_by_project ON deliverables (project_id, position)",
        """CREATE TABLE project_resources (
            project_id  TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            role        TEXT NOT NULL DEFAULT 'evidence',
            added_at    TEXT NOT NULL,
            PRIMARY KEY (project_id, resource_id)
        ) WITHOUT ROWID""",
        "CREATE INDEX project_resources_by_resource ON project_resources (resource_id)",
        "ALTER TABLE resources ADD COLUMN meta TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta))",
        "ALTER TABLE resources ADD COLUMN owner_project_id TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX events_project_id ON events (json_extract(data, '$.project_id')) WHERE type = 'run_started'",
        "CREATE INDEX events_project_run ON events (json_extract(data, '$.project_run')) WHERE type = 'run_started'",
    ],
}


class Database:
    """A single shared connection guarded by a lock (the app is one process: API threads + one worker thread)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)  # autocommit
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        version = self.version()
        latest = max(MIGRATIONS, default=1)
        if version > latest:  # opened by older code: do not guess what the newer tables mean
            raise RuntimeError(f"database schema v{version} is newer than this code (v{latest}): update the application")
        if version < 1:  # new database, or one made before versions existed: both are brought to the v1 baseline (idempotent)
            self.conn.executescript(SCHEMA)
            for table, cols in ADDED_COLUMNS.items():
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for name, ddl in cols.items():
                    if name not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            self.conn.execute("PRAGMA user_version = 1")
            version = 1
        for v in sorted(MIGRATIONS):
            if v <= version:
                continue
            self.conn.execute("BEGIN")
            try:
                for statement in MIGRATIONS[v]:
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version = {v}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def open_db(root: "str | Path | Database") -> Database:
    """A directory means `<dir>/aiops.db`; a path ending in `.db` is the file itself."""
    if isinstance(root, Database):
        return root
    p = Path(root)
    return Database(p if p.suffix == ".db" else p / "aiops.db")
