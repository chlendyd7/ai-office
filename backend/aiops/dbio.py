"""Move data in and out of the database as plain, deterministic text files.

    python -m aiops.dbio export <dir>    # data/aiops.db  ->  <dir>/events/<run_id>.jsonl, <dir>/queue.jsonl
    python -m aiops.dbio import <dir>    # <dir> -> data/aiops.db   (idempotent: rows that already exist are skipped)

The text layout is also the old file-per-run format, so a legacy `runs/` folder imports as it is. Export is
deterministic (same rows -> byte-identical files), so it diffs and merges sensibly in git, unlike the binary .db.
Export only adds and overwrites files; it never deletes, so exporting an empty database cannot wipe a committed export.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .core.db import Database, open_db


def _dump(e: dict) -> str:
    return json.dumps(e, ensure_ascii=False)


def export_dir(db: "str | Path | Database", out: str | Path) -> dict:
    db, out = open_db(db), Path(out)
    (out / "events").mkdir(parents=True, exist_ok=True)
    counts = {"runs": 0, "events": 0, "queue_events": 0}
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
    return counts


def import_dir(db: "str | Path | Database", src: str | Path) -> dict:
    db, src = open_db(db), Path(src)
    run_dir = src / "events" if (src / "events").is_dir() else src  # a legacy runs/ folder is a flat set of <run_id>.jsonl
    counts = {"runs": 0, "events": 0, "queue_events": 0}
    with db.lock:
        db.conn.execute("BEGIN")
        try:
            for f in sorted(run_dir.glob("*.jsonl")):
                if f.name == "queue.jsonl":
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
            db.conn.execute("COMMIT")
        except BaseException:
            db.conn.execute("ROLLBACK")
            raise
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("export", "import"):
        print(__doc__)
        return 2
    db = Database(Path(__file__).resolve().parents[1] / "data" / "aiops.db")  # backend/data/aiops.db, same as the app
    print((export_dir if argv[0] == "export" else import_dir)(db, argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
