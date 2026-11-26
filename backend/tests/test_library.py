import sqlite3

import pytest
from fastapi.testclient import TestClient

from aiops.api.app import create_app
from aiops.core.db import Database
from aiops.core.engine import Engine
from aiops.core.library import Library, parse_import
from aiops.core.store import RunStore
from aiops.dbio import export_dir, import_dir
from aiops.workflows import REGISTRY
from aiops.workflows.cover_letter import REVIEWER_TEMPLATE, WRITER_TEMPLATE, _reviewer_prompt, _writer_prompt

ITEM = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "저는 데이터에 관심이 많습니다."}
WRITER = WRITER_TEMPLATE + "추가 지시: 정중한 어투\n"  # still contains {{draft}} and {{feedback}}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def wait_done(client, rid, status="awaiting_approval"):
    import time

    end = time.time() + 6
    while client.get(f"/api/runs/{rid}").json()["summary"]["status"] != status:
        assert time.time() < end
        time.sleep(0.05)


# ---- the built-in prompts are exactly what the code used before templates existed ----
def test_default_templates_reproduce_the_original_prompts():
    from aiops.core.config import Criterion

    w = _writer_prompt({"job": "J", "question": "Q", "max_chars": 300}, "D", "F")
    assert w == ("당신은 자기소개서 작성 전문가입니다. 아래 문항에 맞게 자기소개서를 개선하세요.\n[지원 직무] J\n[문항] Q\n[분량 제한] 300자 이내\n"
                 "[현재 초안]\nD\n[피드백]\nF\n[규칙] 지원자가 제공한 초안과 사람 피드백에 없는 경험·수치·기술·성과를 지어내지 마세요. "
                 "구체화가 필요하지만 근거가 없으면 [수치 입력 필요]처럼 빈칸으로 표시하세요.\n개선된 자기소개서 본문만 출력하세요.")
    assert "[피드백]\n없음 (첫 작성)\n" in _writer_prompt({}, "D", "")
    r = _reviewer_prompt({"job": "J", "question": "Q"}, "D", [Criterion(name="A", weight=2, description="d")])
    assert r.endswith('[루브릭]\n- A (가중치 2.0): d\n[자기소개서]\nD\n출력 형식: {"criteria":[{"name":"항목명","score":0-10,"feedback":"..."}]}')


def test_a_custom_template_changes_the_prompt_but_the_guards_stay():
    p = _writer_prompt({"job": "J", "question": "Q"}, "초안", "피드백", "역할: 편집자\n초안={{draft}}\n피드백={{feedback}}")
    assert p.startswith("역할: 편집자\n초안=초안\n피드백=피드백\n") and "지어내지 마세요" in p  # the guard cannot be edited away
    r = _reviewer_prompt({"max_chars": 2}, "세글자", [], "심사: {{draft}}")
    assert "[실제 글자 수] 3자" in r and '"criteria"' in r  # length note and parser format are appended by code


def test_values_are_not_re_expanded_and_json_braces_are_safe():
    p = _writer_prompt({"job": "{{question}}", "question": "Q"}, "{{draft}}", "f", "{{job}} / {{draft}} / {\"a\": 1}\n{{feedback}}")
    assert p.startswith('{{question}} / {{draft}} / {"a": 1}\nf')


# ---- datasets and items ----
def test_dataset_and_item_crud(tmp_path):
    lib = Library(Database(tmp_path / "x.db"))
    d = lib.create_dataset(" 시험 ", "cover_letter")
    with pytest.raises(ValueError):
        lib.create_dataset("시험", "cover_letter")  # names are unique
    ids = lib.add_items(d, [{"data": ITEM, "label": "a"}, {"data": {**ITEM, "job": "다른"}}])
    assert [i["data"]["job"] for i in lib.list_items(d)] == ["데이터 엔지니어", "다른"]
    assert lib.list_datasets()[0]["items"] == 2
    lib.update_item(ids[0], {**ITEM, "job": "수정"}, "b")
    assert lib.get_item(ids[0])["data"]["job"] == "수정" and lib.get_item(ids[0])["label"] == "b"
    lib.delete_item(ids[1])
    lib.delete_dataset(d)
    assert lib.list_datasets() == [] and lib.db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_input_is_validated_against_the_workflow_fields(client):
    d = client.post("/api/library/datasets", json={"name": "v"}).json()["id"]
    assert client.post(f"/api/library/datasets/{d}/items", json={"data": {"job": "x"}}).status_code == 400  # question missing
    r = client.post(f"/api/library/datasets/{d}/items", json={"data": {**ITEM, "max_chars": "500", "junk": 1}})
    assert r.status_code == 200
    item = client.get(f"/api/library/datasets/{d}/items").json()[0]["data"]
    assert item["max_chars"] == 500 and "junk" not in item  # number cast, unknown keys dropped
    assert client.post(f"/api/library/datasets/{d}/items", json={"data": {**ITEM, "max_chars": "abc"}}).status_code == 400


def test_csv_import_all_or_nothing(client):
    d = client.post("/api/library/datasets", json={"name": "csv"}).json()["id"]
    good = "﻿job,question,draft,max_chars,label\n백엔드,지원동기,\"초안, 쉼표 포함\",500,첫째\n데이터,성장과정,,,둘째\n"
    assert client.post(f"/api/library/datasets/{d}/import", json={"format": "csv", "text": good}).json() == {"added": 2}
    items = client.get(f"/api/library/datasets/{d}/items").json()
    assert items[0]["label"] == "첫째" and items[0]["data"]["draft"] == "초안, 쉼표 포함" and items[0]["data"]["max_chars"] == 500
    assert "draft" not in items[1]["data"]  # an empty cell is simply absent

    bad = "job,question\n정상,문항\n,문항없는직무\n"
    r = client.post(f"/api/library/datasets/{d}/import", json={"format": "csv", "text": bad})
    assert r.status_code == 400 and "row 2" in r.json()["detail"]
    assert len(client.get(f"/api/library/datasets/{d}/items").json()) == 2  # the good row of the bad file was not added


def test_json_import_shapes_and_limits():
    assert parse_import("json", '[{"job": "a", "question": "b"}]')[0]["raw"] == {"job": "a", "question": "b"}
    assert parse_import("json", '{"items": [{"label": "L", "data": {"job": "a"}}]}')[0] == {"label": "L", "raw": {"job": "a"}}
    for fmt, text in (("json", "not json"), ("json", "[]"), ("json", '{"x": 1}'), ("json", "[1]"), ("csv", ""), ("xml", "a")):
        with pytest.raises(ValueError):
            parse_import(fmt, text)
    with pytest.raises(ValueError, match="at most"):
        parse_import("json", "[" + ",".join(['{"job":"a"}'] * 501) + "]")


# ---- prompts ----
def test_prompt_versions_are_immutable_and_numbered(tmp_path):
    lib = Library(Database(tmp_path / "x.db"))
    assert [lib.save_prompt("cover_letter", "writer", "격식체", f"v{i}", note=f"n{i}") for i in (1, 2, 3)] == [1, 2, 3]
    assert lib.get_prompt("격식체")["body"] == "v3" and lib.get_prompt("격식체", 1)["body"] == "v1"
    assert [v["version"] for v in lib.list_prompts()[0]["versions"]] == [1, 2, 3]
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        lib.db.conn.execute("UPDATE prompts SET body = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        lib.db.conn.execute("DELETE FROM prompts")
    with pytest.raises(ValueError, match="already exists"):
        lib.save_prompt("cover_letter", "reviewer", "격식체", "x")  # a name belongs to one role
    for bad in ("", "a@b"):
        with pytest.raises(ValueError):
            lib.save_prompt("cover_letter", "writer", bad or "n", "x" if bad else "  ")


def test_saving_a_prompt_applies_the_same_checks_a_run_does(client):
    ok = {"workflow": "cover_letter", "role": "writer", "name": "p", "body": WRITER}
    assert client.post("/api/library/prompts", json=ok).json() == {"name": "p", "version": 1}
    assert client.post("/api/library/prompts", json=ok).json()["version"] == 2
    def bad(body, role="writer"):
        return client.post("/api/library/prompts", json={**ok, "name": "q", "role": role, "body": body})
    assert "unknown variable" in bad("{{draft}} {{feedback}} {{nope}}").json()["detail"]
    assert "must contain {{feedback}}" in bad("{{draft}}").json()["detail"]
    assert bad("x", role="nobody").status_code == 400


# ---- a run takes its own copy ----
def test_run_snapshots_prompt_text_and_reference(client):
    client.post("/api/library/prompts", json={"workflow": "cover_letter", "role": "writer", "name": "p", "body": WRITER})
    rid = client.post("/api/runs", json={"workflow": "cover_letter", "input": ITEM, "prompt_refs": {"writer": "p"}}).json()["run_id"]
    wait_done(client, rid)
    ev = client.get(f"/api/runs/{rid}").json()["events"]
    cfg = ev[0]["config"]
    assert cfg["prompts"] == {"writer": WRITER} and cfg["prompt_refs"] == {"writer": "p@1"}
    first = next(e for e in ev if e["type"] == "llm_call" and e["purpose"] == "write")
    assert "추가 지시: 정중한 어투" in first["prompt"]

    # a later version does not change the finished run's record
    client.post("/api/library/prompts", json={"workflow": "cover_letter", "role": "writer", "name": "p", "body": WRITER_TEMPLATE + "다른 지시\n"})
    again = client.get(f"/api/runs/{rid}").json()["events"][0]["config"]
    assert again["prompt_refs"] == {"writer": "p@1"} and again["prompts"]["writer"] == WRITER


def test_a_name_means_latest_now_and_an_explicit_version_pins(client):
    for body in (WRITER, WRITER_TEMPLATE + "v2\n"):
        client.post("/api/library/prompts", json={"workflow": "cover_letter", "role": "writer", "name": "p", "body": body})
    def start(ref):
        rid = client.post("/api/runs", json={"workflow": "cover_letter", "input": ITEM, "prompt_refs": {"writer": ref}}).json()["run_id"]
        return client.get(f"/api/runs/{rid}").json()["events"][0]["config"]["prompt_refs"]["writer"]
    assert start("p") == "p@2" and start("p@1") == "p@1"
    assert client.post("/api/runs", json={"workflow": "cover_letter", "input": ITEM, "prompt_refs": {"writer": "p@9"}}).status_code == 400
    assert client.post("/api/runs", json={"workflow": "cover_letter", "input": ITEM, "prompt_refs": {"reviewer": "p"}}).status_code == 400  # wrong role


def test_rerun_with_another_prompt_version_keeps_input_and_links_parent(client):
    for body in (WRITER, WRITER_TEMPLATE + "v2\n"):
        client.post("/api/library/prompts", json={"workflow": "cover_letter", "role": "writer", "name": "p", "body": body})
    a = client.post("/api/runs", json={"workflow": "cover_letter", "input": ITEM, "prompt_refs": {"writer": "p@1"}}).json()["run_id"]
    wait_done(client, a)
    b = client.post(f"/api/runs/{a}/rerun", json={"prompt_refs": {"writer": "p@2"}}).json()["run_id"]
    wait_done(client, b)
    sb = client.get(f"/api/runs/{b}").json()
    assert sb["summary"]["parent"] == a and sb["events"][0]["input"] == ITEM
    assert sb["events"][0]["config"]["prompt_refs"] == {"writer": "p@2"}


def test_hand_written_prompts_in_overrides_are_checked_too(client):
    r = client.post("/api/runs", json={"workflow": "cover_letter", "input": ITEM, "overrides": {"prompts": {"writer": "{{oops}}"}}})
    assert r.status_code == 400 and "unknown variable" in r.json()["detail"]


# ---- dataset -> queue ----
def test_enqueue_a_dataset_snapshots_each_item_and_the_prompt(client):
    client.post("/api/library/prompts", json={"workflow": "cover_letter", "role": "writer", "name": "p", "body": WRITER})
    d = client.post("/api/library/datasets", json={"name": "q"}).json()["id"]
    rows = "job,question,draft\nA,문항1,초안1\nB,문항2,초안2\nC,문항3,초안3\n"
    client.post(f"/api/library/datasets/{d}/import", json={"format": "csv", "text": rows})
    items = client.get(f"/api/library/datasets/{d}/items").json()

    r = client.post(f"/api/library/datasets/{d}/enqueue", json={"prompt_refs": {"writer": "p"}, "overrides": {"budget_usd": 0.3},
                                                                "item_ids": [items[0]["id"], items[2]["id"]]})
    assert len(r.json()["job_ids"]) == 2
    jobs = client.get("/api/queue").json()["jobs"]
    assert sorted(j["input"]["job"] for j in jobs) == ["A", "C"]
    assert all(j["overrides"]["prompt_refs"] == {"writer": "p@1"} and j["overrides"]["budget_usd"] == 0.3 for j in jobs)

    client.post(f"/api/library/datasets/{d}/items", json={"data": ITEM})
    n_before = len(client.get("/api/queue").json()["jobs"])
    bad = client.post(f"/api/library/datasets/{d}/enqueue", json={"overrides": {"rubric": []}})  # invalid settings: nothing is queued
    assert bad.status_code == 400 and len(client.get("/api/queue").json()["jobs"]) == n_before
    assert client.post(f"/api/library/datasets/{d}/enqueue", json={"item_ids": ["nope"]}).status_code == 400


# ---- the library travels with the export ----
def test_library_survives_export_and_import_and_updates_flow_one_way(tmp_path):
    a = Library(Database(tmp_path / "a.db"))
    d = a.create_dataset("셋", "cover_letter")
    iid = a.add_items(d, [{"data": ITEM, "label": "L"}])[0]
    a.save_prompt("cover_letter", "writer", "p", WRITER)
    export_dir(a.db, tmp_path / "out")

    b = Library(Database(tmp_path / "b.db"))
    got = import_dir(b.db, tmp_path / "out")
    assert (got["datasets"], got["items"], got["prompts"]) == (1, 1, 1)
    assert b.get_item(iid) == a.get_item(iid) and b.get_prompt("p") == a.get_prompt("p")

    a.update_item(iid, {**ITEM, "job": "고침"})  # edited on A after the first export
    export_dir(a.db, tmp_path / "out")
    assert import_dir(b.db, tmp_path / "out")["items"] == 1 and b.get_item(iid)["data"]["job"] == "고침"
    assert import_dir(b.db, tmp_path / "out")["items"] == 0  # same copy again: nothing to do

    b.update_item(iid, {**ITEM, "job": "B에서 더 늦게 고침"})
    assert import_dir(b.db, tmp_path / "out")["items"] == 0 and b.get_item(iid)["data"]["job"] == "B에서 더 늦게 고침"  # older copies never overwrite newer ones


def test_a_diverged_prompt_version_fails_the_import_loudly(tmp_path):
    a, b = Library(Database(tmp_path / "a.db")), Library(Database(tmp_path / "b.db"))
    a.save_prompt("cover_letter", "writer", "p", WRITER)
    b.save_prompt("cover_letter", "writer", "p", WRITER_TEMPLATE + "다른 PC에서\n")  # same name@1, different text
    export_dir(a.db, tmp_path / "out")
    with pytest.raises(ValueError, match="different text"):
        import_dir(b.db, tmp_path / "out")
