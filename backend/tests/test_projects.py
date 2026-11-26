import hashlib
import time
import json
import sqlite3

import pytest

from aiops.core import db as dbmod
from aiops.core.db import SCHEMA, Database
from aiops.core.integrity import integrity_problems
from aiops.core.library import Library
from aiops.core.projects import Projects
from aiops.dbio import export_dir, import_dir
from aiops.offices import OFFICES
from aiops.workflows.base import clean_fields


@pytest.fixture
def px(tmp_path):
    return Projects(Database(tmp_path / "x.db"))


def mk(px, title="A사 · 데이터 엔지니어"):
    return px.create_project("career", title, {"company": "A사", "role": "데이터 엔지니어"})


# ---- schema versions ----
def test_a_new_database_reaches_the_latest_version_and_reopening_changes_nothing(tmp_path):
    db = Database(tmp_path / "x.db")
    assert db.version() == max(dbmod.MIGRATIONS) == 3
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "deliverables", "project_resources", "resources", "events"} <= tables
    db.conn.execute("INSERT INTO projects (id, office, title, created_at, updated_at) VALUES ('p', 'career', 't', 'x', 'x')")
    db.close()
    again = Database(tmp_path / "x.db")
    assert again.version() == 3 and again.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_a_v1_database_is_upgraded_in_place_and_keeps_every_row(tmp_path):
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)  # the baseline everything shipped with before projects existed
    old.execute("INSERT INTO resources (id, name, source_type, text, created_at, updated_at) VALUES ('r1', '옛 이력서', 'txt', '내용', 't', 't')")
    old.execute("INSERT INTO events (run_id, seq, ts, type, data) VALUES ('run1', 0, 't', 'run_started', '{}')")
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    db = Database(path)
    assert db.version() == 3
    r = Library(db).get_resource("r1")
    assert (r["name"], r["text"], r["meta"], r["owner_project_id"]) == ("옛 이력서", "내용", {}, "")
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_a_failing_migration_applies_nothing_and_keeps_the_old_version(tmp_path, monkeypatch):
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    monkeypatch.setattr(dbmod, "MIGRATIONS", {2: ["CREATE TABLE half_done (id TEXT)", "THIS IS NOT SQL"]})
    with pytest.raises(sqlite3.Error):
        Database(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 1  # not bumped
    assert check.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone() is None  # and the first statement was undone


def test_a_database_from_newer_code_is_refused(tmp_path):
    path = tmp_path / "future.db"
    Database(path).close()
    c = sqlite3.connect(path)
    c.execute("PRAGMA user_version = 99")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        Database(path)


def test_the_task_and_project_lookups_use_their_indexes(tmp_path):
    db = Database(tmp_path / "x.db")
    for col, idx in (("project_id", "events_project_id"), ("project_run", "events_project_run")):
        plan = " ".join(str(r) for r in db.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT run_id FROM events WHERE type = 'run_started' AND json_extract(data, '$.{col}') = 'x'"))
        assert idx in plan


# ---- projects ----
def test_project_crud_and_listing(px):
    a = mk(px, "A사")
    b = px.create_project("career", "B사", {"company": "B"})
    time.sleep(0.01)  # timestamps have millisecond resolution: make "later" unambiguous
    px.add_deliverable(a, "answer", "지원동기")
    d2 = px.add_deliverable(a, "answer", "성장과정")
    px.set_final(d2, "승인된 글")
    rows = {r["title"]: r for r in px.list_projects()}
    assert rows["A사"]["deliverables"] == 2 and rows["A사"]["finals"] == 1 and rows["B사"]["deliverables"] == 0
    assert [r["title"] for r in px.list_projects()][0] == "A사"  # most recently touched first
    time.sleep(0.01)
    px.update_project(b, notes="메모")
    assert [r["title"] for r in px.list_projects()][0] == "B사"
    c = px.create_project("career", "C사")  # created right after B: when the clock cannot tell them apart, the newer one is still first
    px.db.conn.execute("UPDATE projects SET updated_at = 'same'")
    assert [r["title"] for r in px.list_projects()][:2] == ["C사", "B사"] and c
    assert px.get_project(b)["notes"] == "메모" and px.get_project(a)["brief"] == {"company": "A사", "role": "데이터 엔지니어"}
    assert [r["id"] for r in px.list_projects("career")] == [r["id"] for r in px.list_projects()] and px.list_projects("code") == []
    for bad in (("", "t"), ("career", "  ")):
        with pytest.raises(ValueError):
            px.create_project(*bad)
    with pytest.raises(KeyError):
        px.get_project("nope")


def test_deliverables_keep_their_order_and_reordering_must_be_complete(px):
    p = mk(px)
    ids = [px.add_deliverable(p, "answer", t, {"max_chars": 500}) for t in ("가", "나", "다")]
    assert [d["position"] for d in px.list_deliverables(p)] == [0, 1, 2]
    px.delete_deliverable(ids[1])
    assert [d["title"] for d in px.list_deliverables(p)] == ["가", "다"]
    assert px.add_deliverable(p, "answer", "라") and [d["title"] for d in px.list_deliverables(p)] == ["가", "다", "라"]  # no position reuse clash
    order = [d["id"] for d in px.list_deliverables(p)]
    assert [d["title"] for d in px.reorder(p, order[::-1])] == ["라", "다", "가"]
    for bad in (order[:-1], order + [order[0]], order + ["x"]):
        with pytest.raises(ValueError, match="exactly once"):
            px.reorder(p, bad)
    assert [d["title"] for d in px.list_deliverables(p)] == ["라", "다", "가"]  # a refused reorder changed nothing
    assert px.update_deliverable(order[0], title="새 제목", spec={"max_chars": 300})["spec"] == {"max_chars": 300}


def test_a_final_is_written_on_approval_edited_by_a_human_and_can_be_cleared(px):
    d = px.add_deliverable(mk(px), "answer", "지원동기")
    assert px.get_deliverable(d)["final_content"] is None
    with pytest.raises(ValueError, match="no approved final"):
        px.edit_final(d, "아직 없음")
    f = px.set_final(d, "AI가 쓰고 사람이 승인", {"chars": 12}, run_id="run-1")
    assert (f["final_content"], f["final_meta"], f["final_run_id"]) == ("AI가 쓰고 사람이 승인", {"chars": 12}, "run-1") and f["final_at"]
    e = px.edit_final(d, "사람이 다듬음", {"chars": 7})
    assert e["final_content"] == "사람이 다듬음" and e["final_run_id"] == "run-1" and e["final_at"] == f["final_at"]  # the source run stays recorded
    assert px.edit_final(d, "메타 그대로")["final_meta"] == {"chars": 7}
    assert px.set_final(d, "다시 승인", run_id="run-2")["final_run_id"] == "run-2"
    c = px.clear_final(d)
    assert c["final_content"] is None and c["final_at"] is None and c["final_meta"] == {}
    with pytest.raises(ValueError):
        px.set_final(d, "   ")


# ---- the material a project works from ----
def test_only_verified_resources_of_my_library_or_this_project_can_be_attached(px):
    lib = px.library
    p, other = mk(px, "A"), mk(px, "B")
    good = lib.add_resource("내 경험", "수치 8시간에서 2시간")
    unverified = lib.add_resource("변환본", "x", source_type="pdf", converted_by="llm")
    theirs = px.add_own_resource(other, "B사 직무 설명", "요구사항", "brief")

    px.attach_resource(p, good, "evidence")
    with pytest.raises(ValueError, match="검수 전"):
        px.attach_resource(p, unverified)
    with pytest.raises(ValueError, match="another project"):
        px.attach_resource(p, theirs)
    with pytest.raises(KeyError):
        px.attach_resource(p, "nope")
    assert [(r["name"], r["role"]) for r in px.list_project_resources(p)] == [("내 경험", "evidence")]

    px.attach_resource(p, good, "reference")  # attaching again only changes the role
    assert [r["role"] for r in px.list_project_resources(p)] == ["reference"]
    lib.update_resource(unverified, verified=True)  # after a human has checked it against the original
    px.attach_resource(p, unverified)
    assert len(px.list_project_resources(p)) == 2 and "text" not in px.list_project_resources(p)[0]
    px.detach_resource(p, good)
    assert [r["name"] for r in px.list_project_resources(p)] == ["변환본"]


def test_a_resource_in_use_cannot_be_deleted_until_it_is_taken_out(px):
    p = mk(px, "A사 지원")
    r = px.library.add_resource("경험", "내용")
    px.attach_resource(p, r)
    assert px.projects_using(r) == [{"id": p, "title": "A사 지원"}]
    with pytest.raises(ValueError, match="A사 지원"):  # the message names who is using it
        px.library.delete_resource(r)
    assert px.library.get_resource(r)["name"] == "경험"
    px.detach_resource(p, r)
    px.library.delete_resource(r)
    with pytest.raises(KeyError):
        px.library.get_resource(r)


def test_project_resources_stay_out_of_the_library_until_promoted(px):
    p = mk(px)
    lib = px.library
    mine = lib.add_resource("내 이력서", "경력")
    jd = px.add_own_resource(p, "직무 설명", "SQL, Python", "brief")
    assert [r["id"] for r in lib.list_resources()] == [mine]  # the library list does not show the job description
    assert [r["id"] for r in lib.list_resources(owner_project_id=p)] == [jd]
    assert [r["role"] for r in px.list_project_resources(p)] == ["brief"]
    assert lib.promote_resource(jd)["owner_project_id"] == "" and {r["id"] for r in lib.list_resources()} == {mine, jd}


def test_an_unverified_own_resource_is_kept_but_not_attached_until_checked(px):
    p = mk(px)
    jd = px.add_own_resource(p, "공고 스크린샷", "변환된 직무 설명", "brief", source_type="image", converted_by="llm", convert_run_id="conv-1")
    assert px.list_project_resources(p) == [] and px.library.get_resource(jd)["review_status"] == "unverified"
    px.library.update_resource(jd, verified=True)
    px.attach_resource(p, jd, "brief")
    assert [r["name"] for r in px.list_project_resources(p)] == ["공고 스크린샷"]


def test_deleting_a_project_takes_its_own_family_and_nothing_else(px):
    p, q = mk(px, "A"), mk(px, "B")
    mine = px.library.add_resource("내 경험", "내용")
    own = px.add_own_resource(p, "직무 설명", "요구사항", "brief")
    px.attach_resource(p, mine)
    px.attach_resource(q, mine)
    d = px.add_deliverable(p, "answer", "문항")
    px.set_final(d, "최종")
    other_d = px.add_deliverable(q, "answer", "다른 문항")
    px.db.conn.execute("INSERT INTO events (run_id, seq, ts, type, data) VALUES ('run1', 0, 't', 'run_started', '{}')")

    assert px.delete_project(p) == {"deliverables": 1, "project_resources": 2, "own_resources": 1, "projects": 1}
    with pytest.raises(KeyError):
        px.get_project(p)
    with pytest.raises(KeyError):
        px.library.get_resource(own)
    assert px.library.get_resource(mine)["name"] == "내 경험"  # a library resource survives
    assert [r["id"] for r in px.list_project_resources(q)] == [mine] and px.get_deliverable(other_d)["project_id"] == q  # so does the other project
    assert px.db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1  # run records are history
    assert integrity_problems(px.db) == []


def test_a_run_snapshot_copies_the_text_with_a_hash_and_refuses_unverified_material(px):
    p = mk(px)
    r = px.library.add_resource("경험", "처리 시간을 8시간에서 2시간으로 단축")
    px.attach_resource(p, r, "evidence")
    d1, d2 = px.add_deliverable(p, "answer", "지원동기", {"max_chars": 500}), px.add_deliverable(p, "answer", "성장과정")
    snap = px.snapshot_inputs(p)
    assert snap["project"]["id"] == p and [d["id"] for d in snap["deliverables"]] == [d1, d2] and snap["deliverables"][0]["spec"] == {"max_chars": 500}
    assert snap["resources"] == [{"id": r, "name": "경험", "role": "evidence", "text": "처리 시간을 8시간에서 2시간으로 단축",
                                  "sha256": hashlib.sha256("처리 시간을 8시간에서 2시간으로 단축".encode()).hexdigest()}]
    px.library.update_resource(r, text="나중에 고친 내용")
    assert snap["resources"][0]["text"] == "처리 시간을 8시간에서 2시간으로 단축"  # the copy does not follow later edits
    assert px.snapshot_inputs(p)["resources"][0]["sha256"] != snap["resources"][0]["sha256"]  # a new snapshot shows the change
    px.library.update_resource(r, verified=False)  # signed off, attached, then the sign-off was withdrawn
    with pytest.raises(ValueError, match="검수 전"):
        px.snapshot_inputs(p)


# ---- no foreign keys: the audit finds what a crash or a bad import could leave ----
def test_integrity_audit_is_clean_for_healthy_data_and_names_every_kind_of_orphan(px):
    p = mk(px)
    r = px.library.add_resource("경험", "내용")
    d = px.add_deliverable(p, "answer", "문항")
    px.attach_resource(p, r)
    assert integrity_problems(px.db) == []

    c = px.db.conn  # damage it the way a half-finished delete or an import from another machine could
    c.execute("INSERT INTO deliverables (id, project_id, position, kind, title, created_at, updated_at) VALUES ('d9', 'gone', 0, 'answer', 't', 'x', 'x')")
    c.execute("INSERT INTO project_resources VALUES ('gone', ?, 'evidence', 'x')", (r,))
    c.execute("INSERT INTO project_resources VALUES (?, 'gone-res', 'evidence', 'x')", (p,))
    c.execute("INSERT INTO resources (id, name, source_type, text, created_at, updated_at, owner_project_id) VALUES ('r9', 'n', 'txt', 't', 'x', 'x', 'gone')")
    c.execute("INSERT INTO items (id, dataset_id, data, created_at, updated_at) VALUES ('i9', 'gone-ds', '{}', 'x', 'x')")
    c.execute("UPDATE resources SET convert_run_id = 'no-such-run' WHERE id = ?", (r,))
    c.execute("UPDATE deliverables SET final_run_id = 'no-such-run' WHERE id = ?", (d,))
    c.execute("UPDATE resources SET owner_project_id = ? WHERE id = 'r9'", (px.create_project("career", "다른 프로젝트"),))
    c.execute("INSERT INTO project_resources VALUES (?, 'r9', 'evidence', 'x')", (p,))
    text = "\n".join(integrity_problems(px.db))
    for needle in ("deliverable d9 belongs to project gone", "project gone links to resource", "links to resource gone-res, which does not exist",
                   "dataset item i9", "names conversion run no-such-run", f"deliverable {d} names run no-such-run", "uses resource r9, which belongs to project"):
        assert needle in text, needle


# ---- what an office declares ----
def test_the_career_office_cleans_briefs_and_specs_and_titles_a_project():
    c = OFFICES["career"]
    brief = c.clean_brief({"company": " A사 ", "role": "데이터 엔지니어", "junk": 1})
    assert brief == {"company": "A사", "role": "데이터 엔지니어"} and c.title_for(brief) == "A사 · 데이터 엔지니어"
    with pytest.raises(ValueError, match="'role' is required"):
        c.clean_brief({"company": "A사"})
    assert c.title_for({}) == "취업 오피스"
    assert c.clean_spec("answer", {"max_chars": "500", "qtype": "지원동기"}) == {"max_chars": 500, "count_mode": "with_spaces", "qtype": "지원동기"}
    assert c.clean_spec("answer", {}) == {"count_mode": "with_spaces", "qtype": "기타"}  # defaults fill in what was left out
    for bad in ({"count_mode": "bytes"}, {"qtype": "자유"}, {"max_chars": "0"}, {"max_chars": "많이"}):
        with pytest.raises(ValueError):
            c.clean_spec("answer", bad)
    with pytest.raises(ValueError, match="unknown deliverable kind"):
        c.clean_spec("patch", {})
    c.check_role("evidence")
    with pytest.raises(ValueError, match="unknown resource role"):
        c.check_role("secret")


def test_field_cleaning_supports_defaults_and_options_without_changing_old_fields():
    fields = [{"name": "a", "type": "text", "required": True}, {"name": "m", "type": "select", "options": ["x", "y"], "default": "x"}]
    assert clean_fields(fields, {"a": " v "}) == {"a": "v", "m": "x"} and clean_fields(fields, {"a": "v", "m": "y"})["m"] == "y"
    with pytest.raises(ValueError, match="must be one of x, y"):
        clean_fields(fields, {"a": "v", "m": "z"})


# ---- travelling ----
def test_projects_travel_with_the_export_as_readable_objects_and_the_newer_edit_wins(tmp_path):
    a, b = Projects(Database(tmp_path / "a.db")), Projects(Database(tmp_path / "b.db"))
    p = a.create_project("career", "A사", {"company": "A사", "role": "엔지니어"})
    d = a.add_deliverable(p, "answer", "지원동기", {"max_chars": 500})
    a.db.conn.execute("INSERT INTO events (run_id, seq, ts, type, data) VALUES ('run-1', 0, 't', 'run_started', '{}')")  # the run the final came from
    a.set_final(d, "최종본", {"chars": 3}, "run-1")
    r = a.library.add_resource("경험", "내용")
    own = a.add_own_resource(p, "직무 설명", "SQL", "brief", meta={"sources": []})
    a.attach_resource(p, r)
    export_dir(a.db, tmp_path / "out")
    row = json.loads((tmp_path / "out" / "projects.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["brief"] == {"company": "A사", "role": "엔지니어"}  # an object, not a JSON string inside JSON

    got = import_dir(b.db, tmp_path / "out")
    assert (got["projects"], got["deliverables"], got["project_resources"]) == (1, 1, 2)
    assert b.get_project(p) == a.get_project(p) and b.get_deliverable(d) == a.get_deliverable(d)
    assert {x["id"] for x in b.list_project_resources(p)} == {r, own} and b.library.get_resource(own)["owner_project_id"] == p
    assert integrity_problems(b.db) == []

    a.edit_final(d, "더 다듬은 최종본")
    export_dir(a.db, tmp_path / "out")
    assert import_dir(b.db, tmp_path / "out")["deliverables"] == 1 and b.get_deliverable(d)["final_content"] == "더 다듬은 최종본"
    b.edit_final(d, "B에서 더 늦게 고침")
    assert import_dir(b.db, tmp_path / "out")["deliverables"] == 0 and b.get_deliverable(d)["final_content"] == "B에서 더 늦게 고침"
