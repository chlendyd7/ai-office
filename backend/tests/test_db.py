import sqlite3
import threading

import pytest

from aiops.core.db import Database
from aiops.core.engine import Engine
from aiops.core.jobqueue import JobQueue
from aiops.core.store import RunStore
from aiops.core.summary import summarize
from aiops.dbio import export_dir, import_dir
from aiops.workflows import REGISTRY

INPUT = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "저는 데이터에 관심이 많습니다."}


def make_data(tmp_path):
    """A database with a finished run, a run waiting at a step gate, and a queue entry."""
    db = Database(tmp_path / "src.db")
    eng = Engine(RunStore(db), REGISTRY)
    a = eng.start("cover_letter", INPUT)
    eng.decide(a, "approve", "좋음")
    b = eng.start("cover_letter", INPUT, {"step_mode": True})
    JobQueue(db).enqueue([{"workflow": "cover_letter", "input": INPUT}])
    return db, eng.store, (a, b)


def test_run_record_is_append_only_at_the_database_level(tmp_path):
    store = RunStore(tmp_path)
    store.append("r1", "run_started", config={}, input={}, workflow="x")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.conn.execute("UPDATE events SET type = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.conn.execute("DELETE FROM events")
    JobQueue(store.db).enqueue([{"workflow": "cover_letter", "input": {}}])  # row-level triggers need a row to fire on
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store.db.conn.execute("DELETE FROM queue_events")
    assert [e["type"] for e in store.read("r1")] == ["run_started"]


def test_events_survive_reopening_the_file_and_seq_continues(tmp_path):
    store = RunStore(tmp_path)
    store.append("r1", "a", n=1)
    store.append("r1", "b", n=2)
    store.db.close()
    again = RunStore(tmp_path)
    assert again.append("r1", "c")["seq"] == 2
    assert [(e["seq"], e["type"]) for e in again.read("r1")] == [(0, "a"), (1, "b"), (2, "c")]
    assert again.list_runs() == ["r1"]


def test_unknown_run_is_a_key_error(tmp_path):
    with pytest.raises(KeyError):
        RunStore(tmp_path).read("nope")


def test_concurrent_appends_never_reuse_a_seq(tmp_path):
    store = RunStore(tmp_path)
    def work():
        for _ in range(25):
            store.append("r1", "tick")
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert [e["seq"] for e in store.read("r1")] == list(range(100))


def test_export_then_import_reproduces_everything(tmp_path):
    db, store, (a, b) = make_data(tmp_path)
    counts = export_dir(db, tmp_path / "out")
    assert counts["runs"] == 2 and counts["queue_events"] == 1

    fresh = Database(tmp_path / "fresh.db")
    got = import_dir(fresh, tmp_path / "out")
    assert got == counts
    copy = RunStore(fresh)
    assert copy.list_runs() == store.list_runs()
    for rid in (a, b):
        assert copy.read(rid) == store.read(rid)  # identical events, order and values
        assert summarize(copy.read(rid)) == summarize(store.read(rid))
    assert JobQueue(fresh).jobs() == JobQueue(db).jobs()


def test_import_is_idempotent_and_export_is_deterministic(tmp_path):
    db, _, _ = make_data(tmp_path)
    export_dir(db, tmp_path / "o1")
    export_dir(db, tmp_path / "o2")
    files = sorted(p.relative_to(tmp_path / "o1") for p in (tmp_path / "o1").rglob("*.jsonl"))
    assert files and all((tmp_path / "o1" / f).read_bytes() == (tmp_path / "o2" / f).read_bytes() for f in files)

    fresh = Database(tmp_path / "fresh.db")
    import_dir(fresh, tmp_path / "o1")
    again = import_dir(fresh, tmp_path / "o1")  # second time: nothing new
    assert again == {"runs": 0, "events": 0, "queue_events": 0}


def test_export_never_deletes_what_is_already_there(tmp_path):
    db, _, (a, _) = make_data(tmp_path)
    export_dir(db, tmp_path / "out")
    export_dir(Database(tmp_path / "empty.db"), tmp_path / "out")  # exporting an empty database
    assert (tmp_path / "out" / "events" / f"{a}.jsonl").exists()


def test_legacy_run_folder_imports_as_it_is(tmp_path):
    legacy = tmp_path / "runs"
    legacy.mkdir()
    (legacy / "20260101-000000-abc123.jsonl").write_text(
        '{"seq": 0, "ts": "2026-01-01T00:00:00.000+00:00", "type": "run_started", "run_id": "20260101-000000-abc123", '
        '"workflow": "cover_letter", "config": {}, "input": {"job": "한글"}, "parent": null}\n', encoding="utf-8")
    store = RunStore(tmp_path / "db")
    assert import_dir(store.db, legacy)["runs"] == 1
    ev = store.read("20260101-000000-abc123")
    assert ev[0]["input"] == {"job": "한글"} and ev[0]["seq"] == 0


def test_a_failed_import_changes_nothing(tmp_path):
    src = tmp_path / "src" / "events"
    src.mkdir(parents=True)
    (src / "r1.jsonl").write_text('{"seq": 0, "ts": "t", "type": "a"}\n', encoding="utf-8")
    (src / "r2.jsonl").write_text('{"seq": 0, "ts": "t", "type": "a"}\nnot json\n', encoding="utf-8")
    store = RunStore(tmp_path / "db")
    with pytest.raises(ValueError):
        import_dir(store.db, tmp_path / "src")
    assert store.list_runs() == []  # r1 was rolled back together with the broken r2
