import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiops.api.app import PRICING_FILE, create_app
from aiops.core.engine import Engine
from aiops.core.pricing import load_prices
from aiops.core.projects import Projects
from aiops.core.store import RunStore
from aiops.llm.base import LLMResult
from aiops.offices.career import measure_answer
from aiops.workflows import REGISTRY
from aiops.workflows.base import count_chars

FIX = Path(__file__).parent / "fixtures"
SCAN = (FIX / "scanned_like.pdf").read_bytes()


class Stub:
    model = "claude-haiku-4-5"

    def complete(self, *, agent, purpose, prompt, meta, attachments=None):
        return LLMResult("=== 쪽 1 ===\n" + "변환된 직무 설명입니다. " * 6, 500, 100, model=self.model, stop_reason="end_turn")


@pytest.fixture
def client(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda cfg: Stub(), prices=load_prices(PRICING_FILE))
    return TestClient(create_app(tmp_path, engine=eng))


def new_project(c, company="A사", role="데이터 엔지니어", **extra):
    r = c.post("/api/projects", json={"office": "career", "brief": {"company": company, "role": role, **extra}})
    assert r.status_code == 200, r.text
    return r.json()["project"]["id"]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---- offices and projects ----
def test_the_office_declaration_is_what_a_screen_needs_and_is_plain_json(client):
    o = client.get("/api/offices").json()["career"]
    assert o["label"] == "취업 오피스" and [f["name"] for f in o["brief_fields"]] == ["company", "role", "source_url"]
    assert o["resource_roles"] == ["evidence", "brief", "reference"] and o["stages"][0] == "jd_analysis"
    kind = o["deliverable_kinds"]["answer"]
    assert kind["workflow"] == "cover_letter" and [f["name"] for f in kind["spec_fields"]] == ["max_chars", "count_mode", "qtype", "guidance"]
    assert "measure" not in kind  # functions stay in the code


def test_creating_a_project_titles_it_from_the_brief_and_shows_what_is_missing(client):
    r = client.post("/api/projects", json={"office": "career", "brief": {"company": " A사 ", "role": "데이터 엔지니어", "junk": 1}}).json()
    assert r["project"]["title"] == "A사 · 데이터 엔지니어" and r["project"]["brief"] == {"company": "A사", "role": "데이터 엔지니어"}
    assert r["status"] == "draft" and r["deliverables"] == [] and r["resources"] == []
    assert r["readiness"]["blocking"] == ["자소서 문항이 없습니다", "근거로 쓸 내 리소스가 없습니다 (이력서·경험 정리를 붙이세요)"]
    assert client.post("/api/projects", json={"office": "career", "brief": {"company": "A사"}}).status_code == 400  # role is required
    assert client.post("/api/projects", json={"office": "code", "brief": {}}).status_code == 400
    assert client.get("/api/projects/nope").status_code == 404
    assert [p["title"] for p in client.get("/api/projects", params={"office": "career"}).json()] == ["A사 · 데이터 엔지니어"]


def test_editing_a_project_keeps_the_title_in_step_with_the_brief_unless_one_is_given(client):
    pid = new_project(client)
    assert client.put(f"/api/projects/{pid}", json={"brief": {"company": "B사", "role": "백엔드"}}).json()["project"]["title"] == "B사 · 백엔드"
    assert client.put(f"/api/projects/{pid}", json={"title": "내가 붙인 이름", "notes": "메모"}).json()["project"]["title"] == "내가 붙인 이름"
    assert client.put(f"/api/projects/{pid}", json={"brief": {"company": "C사", "role": "X"}, "title": "그대로"}).json()["project"]["title"] == "그대로"
    assert client.put(f"/api/projects/{pid}", json={"brief": {"company": "C사"}}).status_code == 400


# ---- deliverables ----
def test_deliverables_are_cleaned_by_the_office_ordered_edited_and_deleted(client):
    pid = new_project(client)
    add = lambda title, **spec: client.post(f"/api/projects/{pid}/deliverables", json={"kind": "answer", "title": title, "spec": spec})
    r = add("지원동기를 서술하시오", max_chars="500", qtype="지원동기").json()
    assert r["deliverables"][0]["spec"] == {"max_chars": 500, "count_mode": "with_spaces", "qtype": "지원동기"}  # string cast, defaults filled
    add("성장과정")
    assert add("x", count_mode="bytes").status_code == 400 and add("x", max_chars="0").status_code == 400
    assert client.post(f"/api/projects/{pid}/deliverables", json={"kind": "patch", "title": "t"}).status_code == 400
    assert client.post(f"/api/projects/{pid}/deliverables", json={"kind": "answer", "title": "   "}).status_code == 400

    ids = [d["id"] for d in client.get(f"/api/projects/{pid}").json()["deliverables"]]
    flipped = client.post(f"/api/projects/{pid}/deliverables/order", json={"ids": ids[::-1]}).json()["deliverables"]
    assert [d["title"] for d in flipped] == ["성장과정", "지원동기를 서술하시오"]
    assert client.post(f"/api/projects/{pid}/deliverables/order", json={"ids": ids[:1]}).status_code == 400

    r = client.put(f"/api/deliverables/{ids[0]}", json={"title": "새 문항", "spec": {"max_chars": 300, "count_mode": "without_spaces"}}).json()
    d = next(x for x in r["deliverables"] if x["id"] == ids[0])
    assert d["title"] == "새 문항" and d["spec"]["max_chars"] == 300 and d["spec"]["count_mode"] == "without_spaces"
    assert client.put(f"/api/deliverables/{ids[0]}", json={"spec": {"qtype": "자유"}}).status_code == 400
    assert [d["id"] for d in client.delete(f"/api/deliverables/{ids[0]}").json()["deliverables"]] == [ids[1]]
    assert client.delete(f"/api/deliverables/{ids[0]}").status_code == 404


def test_counting_characters_follows_the_questions_own_rule():
    assert count_chars("가 나\r\n다") == 5 and count_chars("가 나\r\n다", "without_spaces") == 3  # a line break counts once
    with pytest.raises(ValueError):
        count_chars("x", "bytes")
    assert measure_answer({"max_chars": 5, "count_mode": "with_spaces"}, "가 나 다") == {"chars": 5, "count_mode": "with_spaces", "max_chars": 5, "within_limit": True}
    assert measure_answer({"max_chars": 4}, "가 나 다")["within_limit"] is False
    assert measure_answer({"max_chars": 4, "count_mode": "without_spaces"}, "가 나 다")["within_limit"] is True
    assert measure_answer({}, "abc")["within_limit"] is None


def test_an_approved_final_can_be_reworked_and_is_remeasured_by_the_code(client):
    pid = new_project(client)
    did = client.post(f"/api/projects/{pid}/deliverables", json={"kind": "answer", "title": "문항", "spec": {"max_chars": 6, "count_mode": "without_spaces"}}).json()["deliverables"][0]["id"]
    assert client.put(f"/api/deliverables/{did}/final", json={"content": "x"}).status_code == 400  # nothing approved yet
    Projects(client.app.state.queue.db).set_final(did, "승인된 글", {"chars": 4}, "run-1")  # what the pipeline will do when a human approves
    d = lambda r: next(x for x in r.json()["deliverables"] if x["id"] == did)
    r = client.put(f"/api/deliverables/{did}/final", json={"content": "가 나 다 라 마 바 사"})
    assert d(r)["final_content"] == "가 나 다 라 마 바 사" and d(r)["final_run_id"] == "run-1"
    assert d(r)["final_meta"] == {"chars": 7, "count_mode": "without_spaces", "max_chars": 6, "within_limit": False}  # measured, not claimed
    assert d(client.put(f"/api/deliverables/{did}/final", json={"content": "가 나 다"}))["final_meta"]["within_limit"] is True
    assert d(client.delete(f"/api/deliverables/{did}/final"))["final_content"] is None
    assert client.put(f"/api/deliverables/{did}/final", json={"content": "x"}).status_code == 400


# ---- the material a project works from ----
def test_attaching_library_resources_checks_role_review_state_and_existence(client):
    pid = new_project(client)
    good = client.post("/api/library/resources", json={"name": "내 경험", "text": "처리 시간을 8시간에서 2시간으로 단축"}).json()["id"]
    unverified = Projects(client.app.state.queue.db).library.add_resource("변환본", "x", source_type="pdf", converted_by="llm")
    att = lambda rid, role="evidence": client.post(f"/api/projects/{pid}/resources", json={"resource_id": rid, "role": role})
    assert att(good).status_code == 200 and [r["name"] for r in att(good).json()["resources"]] == ["내 경험"]
    assert att(good, "secret").status_code == 400 and att(unverified).status_code == 400 and att("nope").status_code == 404
    assert client.delete(f"/api/projects/{pid}/resources/{good}").json()["resources"] == []


def test_a_resource_a_project_uses_cannot_be_deleted_from_the_library(client):
    pid = new_project(client, "A사 지원")
    rid = client.post("/api/library/resources", json={"name": "내 경험", "text": "내용"}).json()["id"]
    client.post(f"/api/projects/{pid}/resources", json={"resource_id": rid, "role": "evidence"})
    r = client.delete(f"/api/library/resources/{rid}")
    assert r.status_code == 409 and "A사 지원" in r.json()["detail"] and client.get(f"/api/library/resources/{rid}").status_code == 200
    client.delete(f"/api/projects/{pid}/resources/{rid}")
    assert client.delete(f"/api/library/resources/{rid}").status_code == 200


def test_pasted_text_for_a_project_stays_out_of_the_library(client):
    pid = new_project(client)
    mine = client.post("/api/library/resources", json={"name": "내 이력서", "text": "경력"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/resources/paste", json={"name": "직무 설명", "text": "SQL, Python 3년 이상", "role": "brief"}).json()
    assert [(x["name"], x["role"]) for x in r["resources"]] == [("직무 설명", "brief")]
    assert [x["id"] for x in client.get("/api/library/resources").json()] == [mine]  # the job description is not listed with my material
    assert client.post(f"/api/projects/{pid}/resources/paste", json={"name": "x", "text": "y", "role": "secret"}).status_code == 400


def test_a_converted_job_description_joins_the_project_only_after_a_human_checks_it(client):
    pid = new_project(client)
    body = {"filename": "공고.pdf", "data": b64(SCAN), "role": "brief"}
    ask = client.post(f"/api/projects/{pid}/resources/upload", json=body).json()
    assert ask["needs_conversion"] is True and client.get(f"/api/projects/{pid}").json()["resources"] == []  # asked, nothing stored or attached

    r = client.post(f"/api/projects/{pid}/resources/upload", json={**body, "on_bad_pages": "llm"}).json()
    assert r["review_status"] == "unverified" and r["attached"] is False
    assert client.get(f"/api/projects/{pid}").json()["resources"] == []  # kept for checking, not yet used by the project
    assert [x["id"] for x in client.get("/api/library/resources").json()] == []  # and not in the library either

    v = client.put(f"/api/library/resources/{r['id']}", json={"verified": True}).json()
    assert v["attached"] is True  # the check is the act that lets it join, with the role it was uploaded for
    assert [(x["name"], x["role"]) for x in client.get(f"/api/projects/{pid}").json()["resources"]] == [("공고", "brief")]


def test_a_text_file_for_a_project_is_attached_at_once_and_a_library_check_attaches_nothing(client):
    pid = new_project(client)
    r = client.post(f"/api/projects/{pid}/resources/upload", json={"filename": "jd.txt", "data": b64("자격요건: SQL".encode()), "role": "brief"}).json()
    assert r["attached"] is True and r["review_status"] == "verified"
    lib = client.post("/api/library/resources", json={"name": "일반", "text": "내용"}).json()["id"]
    assert "attached" not in client.put(f"/api/library/resources/{lib}", json={"verified": True}).json()


# ---- readiness ----
def test_readiness_names_what_blocks_a_run_and_what_only_weakens_it(client):
    pid = new_project(client)
    mine = client.post("/api/library/resources", json={"name": "내 경험", "text": "수치 8시간"}).json()["id"]
    client.post(f"/api/projects/{pid}/deliverables", json={"kind": "answer", "title": "지원동기를 서술하시오"})
    r = client.post(f"/api/projects/{pid}/resources", json={"resource_id": mine, "role": "evidence"}).json()["readiness"]
    assert r["blocking"] == []
    assert any("직무 설명이 없습니다" in w for w in r["warnings"]) and any("글자 수 제한이 없습니다: 지원동기를 서술하시오" in w for w in r["warnings"])

    client.post(f"/api/projects/{pid}/resources/paste", json={"name": "직무 설명", "text": "SQL", "role": "brief"})
    did = client.get(f"/api/projects/{pid}").json()["deliverables"][0]["id"]
    r = client.put(f"/api/deliverables/{did}", json={"spec": {"max_chars": 500}}).json()["readiness"]
    assert r == {"blocking": [], "warnings": []}

    client.put(f"/api/library/resources/{mine}", json={"verified": False})  # a sign-off withdrawn after the resource was attached
    assert client.get(f"/api/projects/{pid}").json()["readiness"]["blocking"] == ["검수 전 자료가 붙어 있습니다: 내 경험"]


def test_deleting_a_project_removes_its_own_material_and_leaves_the_library(client):
    pid = new_project(client)
    mine = client.post("/api/library/resources", json={"name": "내 이력서", "text": "경력"}).json()["id"]
    client.post(f"/api/projects/{pid}/resources", json={"resource_id": mine, "role": "evidence"})
    client.post(f"/api/projects/{pid}/resources/paste", json={"name": "직무 설명", "text": "SQL", "role": "brief"})
    client.post(f"/api/projects/{pid}/deliverables", json={"kind": "answer", "title": "문항"})
    out = client.delete(f"/api/projects/{pid}").json()["deleted"]
    assert out == {"deliverables": 1, "project_resources": 2, "own_resources": 1, "projects": 1}
    assert client.get(f"/api/projects/{pid}").status_code == 404 and client.delete(f"/api/projects/{pid}").status_code == 404
    assert [x["id"] for x in client.get("/api/library/resources").json()] == [mine]
    assert client.get("/api/office/runs").json() == []  # a project is not a run: the Office screen is unaffected


def test_the_declaration_carries_what_screens_need_to_label_roles_and_options(client):
    o = client.get("/api/offices").json()["career"]
    assert o["role_labels"] == {"evidence": "근거 (내 자료)", "brief": "직무 설명", "reference": "참고"}
    assert (o["default_library_role"], o["default_own_role"]) == ("evidence", "brief")
    mode = next(f for f in o["deliverable_kinds"]["answer"]["spec_fields"] if f["name"] == "count_mode")
    assert mode["option_labels"] == {"with_spaces": "공백 포함", "without_spaces": "공백 제외"}


def test_a_projects_own_resources_can_be_listed_including_those_awaiting_a_check(client):
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/resources/paste", json={"name": "직무 설명", "text": "SQL", "role": "brief"})
    client.post(f"/api/projects/{pid}/resources/upload", json={"filename": "공고.pdf", "data": b64(SCAN), "role": "brief", "on_bad_pages": "llm"})
    own = client.get("/api/library/resources", params={"owner_project_id": pid}).json()
    assert sorted((r["name"], r["review_status"]) for r in own) == [("공고", "unverified"), ("직무 설명", "verified")]
    assert client.get("/api/library/resources").json() == []


def test_the_projects_page_is_served(client):
    r = client.get("/projects")
    assert r.status_code == 200 and "프로젝트" in r.text and r.headers["cache-control"] == "no-store"
