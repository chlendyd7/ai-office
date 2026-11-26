"""Move data in and out of the database as plain, deterministic text files.

    python -m aiops.dbio export <dir> [--allow-git]
                                         # (refuses a folder inside a git repository unless --allow-git: personal data)
                                         # data/aiops.db  ->  <dir>/events/<run_id>.jsonl, queue.jsonl,
                                         #                    datasets.jsonl, items.jsonl, prompts.jsonl, resources.jsonl
    python -m aiops.dbio import <dir>    # <dir> -> data/aiops.db   (idempotent: rows that already exist are skipped;
                                         #  an edited item replaces the older copy; a conflicting prompt version fails)

The text layout is also the old file-per-run format, so a legacy `runs/` folder imports as it is. Export is
deterministic (same rows -> byte-identical files), so it diffs and merges sensibly in git, unlike the binary .db.
Export only adds and overwrites files; it never deletes, so exporting an empty database cannot wipe a committed export.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .core.db import Database, open_db
from .core.library import RESOURCE_DEFAULTS, RESOURCE_FIELDS


NON_RUN_FILES = {"queue.jsonl", "datasets.jsonl", "items.jsonl", "prompts.jsonl", "resources.jsonl"}


def _dump(e: dict) -> str:
    return json.dumps(e, ensure_ascii=False)


def git_root(path: Path) -> Path | None:
    """The nearest enclosing git work tree, if any."""
    p = path.resolve()
    return next((d for d in (p, *p.parents) if (d / ".git").exists()), None)


def export_dir(db: "str | Path | Database", out: str | Path, allow_git: bool = False) -> dict:
    """Refuses to write inside a git work tree unless allow_git: the export holds personal text (resumes, drafts), and a
    repository may be public. Pass allow_git only for a folder in a repository you know is private."""
    db, out = open_db(db), Path(out)
    root = git_root(out)
    if root and not allow_git:
        raise ValueError(f"{out} is inside the git repository {root}. The export contains personal data and a repository may be public: "
                         "export to a folder outside it, or pass --allow-git if that repository is private.")
    (out / "events").mkdir(parents=True, exist_ok=True)
    counts = {"runs": 0, "events": 0, "queue_events": 0, "datasets": 0, "items": 0, "prompts": 0, "resources": 0}
    with db.lock:
        by_run: dict[str, list[str]] = {}
        for run_id, seq, ts, type_, data in db.conn.execute("SELECT run_id, seq, ts, type, data FROM events ORDER BY run_id, seq"):
            by_run.setdefault(run_id, []).append(_dump({"seq": seq, "ts": ts, "type": type_, **json.loads(data)}))
        queue = [_dump({"id": i, "ts": ts, **json.loads(d)}) for i, ts, d in db.conn.execute("SELECT id, ts, data FROM queue_events ORDER BY id")]
    for run_id, lines in by_run.items():
        (out / "events" / f"{run_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        counts["events"] += len(lines)
    (out / "queue.jsonl").write_text("".join(q + "\n" for q in queue), encoding="utf-8", newline="\n")
    counts.update(runs=len(by_run), queue_events=len(queue))
    with db.lock:
        lib = {
            "datasets": [dict(zip(("id", "name", "workflow", "created_at"), r))
                         for r in db.conn.execute("SELECT id, name, workflow, created_at FROM datasets ORDER BY id")],
            "items": [{"id": i, "dataset_id": d, "label": lb, "data": json.loads(js), "created_at": c, "updated_at": u}
                      for i, d, lb, js, c, u in db.conn.execute("SELECT id, dataset_id, label, data, created_at, updated_at FROM items ORDER BY id")],
            "prompts": [dict(zip(("name", "version", "workflow", "role", "body", "note", "created_at"), r))
                        for r in db.conn.execute("SELECT name, version, workflow, role, body, note, created_at FROM prompts ORDER BY name, version")],
            "resources": [dict(zip(RESOURCE_FIELDS, r)) for r in db.conn.execute(f"SELECT {', '.join(RESOURCE_FIELDS)} FROM resources ORDER BY id")],
        }
    for table, rows in lib.items():
        (out / f"{table}.jsonl").write_text("".join(_dump(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
        counts[table] = len(rows)
    return counts


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def _import_library(db: Database, src: Path) -> dict:
    """Datasets and prompts are insert-only; an item is updated when the incoming copy is newer. A prompt version that
    exists with different text is a real conflict (two machines saved different texts as the same version): fail loudly."""
    counts = {"datasets": 0, "items": 0, "prompts": 0, "resources": 0}
    for d in _read_lines(src / "datasets.jsonl"):
        counts["datasets"] += db.conn.execute("INSERT OR IGNORE INTO datasets (id, name, workflow, created_at) VALUES (?, ?, ?, ?)",
                                              (d["id"], d["name"], d["workflow"], d["created_at"])).rowcount
    for it in _read_lines(src / "items.jsonl"):
        if not db.conn.execute("SELECT 1 FROM datasets WHERE id = ?", (it["dataset_id"],)).fetchone():
            raise ValueError(f"item {it['id']} belongs to dataset {it['dataset_id']}, which is not in the database "
                             "(a dataset with the same name but another id may already exist)")
        counts["items"] += db.conn.execute(
            "INSERT INTO items (id, dataset_id, label, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET label = excluded.label, data = excluded.data, updated_at = excluded.updated_at "
            "WHERE excluded.updated_at > items.updated_at",
            (it["id"], it["dataset_id"], it["label"], json.dumps(it["data"], ensure_ascii=False), it["created_at"], it["updated_at"])).rowcount
    for p in _read_lines(src / "prompts.jsonl"):
        have = db.conn.execute("SELECT body FROM prompts WHERE name = ? AND version = ?", (p["name"], p["version"])).fetchone()
        if have and have[0] != p["body"]:
            raise ValueError(f"prompt {p['name']}@{p['version']} exists with different text; save one of them as a new version first")
        counts["prompts"] += db.conn.execute(
            "INSERT OR IGNORE INTO prompts (name, version, workflow, role, body, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (p["name"], p["version"], p["workflow"], p["role"], p["body"], p["note"], p["created_at"])).rowcount
    for r in _read_lines(src / "resources.jsonl"):  # like items: the more recently edited copy wins
        counts["resources"] += db.conn.execute(
            f"INSERT INTO resources ({', '.join(RESOURCE_FIELDS)}) VALUES ({', '.join('?' * len(RESOURCE_FIELDS))}) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, text = excluded.text, review_status = excluded.review_status, "
            "updated_at = excluded.updated_at WHERE excluded.updated_at > resources.updated_at",
            tuple(r.get(f, RESOURCE_DEFAULTS.get(f)) for f in RESOURCE_FIELDS)).rowcount
    return counts


def import_dir(db: "str | Path | Database", src: str | Path) -> dict:
    db, src = open_db(db), Path(src)
    run_dir = src / "events" if (src / "events").is_dir() else src  # a legacy runs/ folder is a flat set of <run_id>.jsonl
    counts = {"runs": 0, "events": 0, "queue_events": 0, "datasets": 0, "items": 0, "prompts": 0, "resources": 0}
    with db.lock:
        db.conn.execute("BEGIN")
        try:
            for f in sorted(run_dir.glob("*.jsonl")):
                if f.name in NON_RUN_FILES:  # a flat legacy folder may sit next to the library files
                    continue
                added = 0
                for line in f.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    e = json.loads(line)
                    seq, ts, type_ = e.pop("seq"), e.pop("ts"), e.pop("type")
                    added += db.conn.execute("INSERT OR IGNORE INTO events (run_id, seq, ts, type, data) VALUES (?, ?, ?, ?, ?)",
                                             (f.stem, seq, ts, type_, json.dumps(e, ensure_ascii=False))).rowcount
                counts["events"] += added
                counts["runs"] += bool(added)
            q = src / "queue.jsonl"
            if q.exists():
                for line in q.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        e = json.loads(line)
                        i, ts = e.pop("id"), e.pop("ts")
                        counts["queue_events"] += db.conn.execute("INSERT OR IGNORE INTO queue_events (id, ts, data) VALUES (?, ?, ?)",
                                                                  (i, ts, json.dumps(e, ensure_ascii=False))).rowcount
            counts.update(_import_library(db, src))
            db.conn.execute("COMMIT")
        except BaseException:
            db.conn.execute("ROLLBACK")
            raise
    return counts


def main(argv: list[str]) -> int:
    allow_git = "--allow-git" in argv
    argv = [a for a in argv if a != "--allow-git"]
    if len(argv) != 2 or argv[0] not in ("export", "import"):
        print(__doc__)
        return 2
    db = Database(Path(__file__).resolve().parents[1] / "data" / "aiops.db")  # backend/data/aiops.db, same as the app
    try:
        print(export_dir(db, argv[1], allow_git) if argv[0] == "export" else import_dir(db, argv[1]))
    except ValueError as e:
        print(f"error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
