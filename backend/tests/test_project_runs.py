import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from aiops.api.app import PRICING_FILE, create_app
from aiops.core.engine import Engine
from aiops.core.pricing import load_prices
from aiops.core.store import RunStore
from aiops.llm.base import LLMResult
from aiops.workflows import REGISTRY
from aiops.workflows.cover_letter import PROJECT_RUBRIC

EVIDENCE = "프로젝트: 로그 수집 파이프라인 구축. 배치 처리 시간을 8시간에서 2시간으로 단축했다. Python과 SQL, Airflow를 썼다."
JD = "자격요건: SQL, Python 3년 이상. 데이터 파이프라인 설계 경험"


class Script:
    """A scripted gateway: writer answers from one list, reviewer scores everything the same."""

    model = "scripted"

    def __init__(self, writes, score=10):
        self.writes, self.score, self.calls = list(writes), score, []

    def complete(self, *, agent, purpose, prompt, meta, attachments=None):
        self.calls.append((purpose, prompt))
        if purpose == "write":
            text = self.writes.pop(0) if len(self.writes) > 1 else self.writes[0]
        else:
            text = json.dumps({"criteria": [{"name": r["name"], "score": self.score, "feedback": "ok"} for r in PROJECT_RUBRIC]}, ensure_ascii=False)
        return LLMResult(text, 100, 50, model="claude-haiku-4-5")


def make(tmp_path, client=None):
    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=(lambda cfg: client) if client else (lambda cfg: __import__("aiops.llm.factory", fromlist=["make_llm"]).make_llm(cfg)),
                 prices=load_prices(PRICING_FILE))
    return TestClient(create_app(tmp_path, engine=eng))


@pytest.fixture
def c(tmp_path):
    return make(tmp_path)


def ready_project(c, spec=None, evidence=EVIDENCE, question="지원동기를 서술하시오"):
    """A project that the office considers ready: a verified evidence resource, a job description, one question."""
    pid = c.post("/api/projects", json={"office": "career", "brief": {"company": "A사", "role": "데이터 엔지니어"}}).json()["project"]["id"]
    rid = c.post("/api/library/resources", json={"name": "내 경험", "text": evidence}).json()["id"]
    c.post(f"/api/projects/{pid}/resources", json={"resource_id": rid, "role": "evidence"})
    c.post(f"/api/projects/{pid}/resources/paste", json={"name": "직무 설명", "text": JD, "role": "brief"})
    did = c.post(f"/api/projects/{pid}/deliverables", json={"kind": "answer", "title": question, "spec": spec or {"max_chars": 500}}).json()["deliverables"][0]["id"]
    return pid, did, rid


def wait(c, run_id, status="awaiting_approval"):
    end = time.time() + 8
    while c.get(f"/api/runs/{run_id}").json()["summary"]["status"] != status:
        assert time.time() < end, c.get(f"/api/runs/{run_id}").json()["summary"]
        time.sleep(0.05)


def draft(c, pid, did, **body):
    r = c.post(f"/api/projects/{pid}/deliverables/{did}/draft", json=body)
    assert r.status_code == 200, r.text
    wait(c, r.json()["run_id"])
    return r.json()["run_id"]


def final_of(c, pid, did):
    return next(d for d in c.get(f"/api/projects/{pid}").json()["deliverables"] if d["id"] == did)


# ---- settings: declared by the office, kept by the project ----
def test_a_project_has_settings_with_free_defaults_that_it_can_change(c):
    pid, _, _ = ready_project(c)
    s = c.get(f"/api/projects/{pid}").json()["settings"]
    assert (s["mode"], s["max_rounds"], s["step_mode"], s["budget_usd"]) == ("fake", 3, False, 0.1)  # nothing here spends money until asked
    r = c.put(f"/api/projects/{pid}", json={"settings": {"mode": "hchat", "budget_usd": "0.05", "step_mode": "true", "pass_score": "7.5", "model_writer": "claude-haiku-4-5"}}).json()
    assert (r["settings"]["mode"], r["settings"]["budget_usd"], r["settings"]["step_mode"], r["settings"]["pass_score"]) == ("hchat", 0.05, True, 7.5)
    r = c.put(f"/api/projects/{pid}", json={"settings": {"max_rounds": 2}}).json()
    assert r["settings"]["max_rounds"] == 2 and r["settings"]["mode"] == "hchat"  # a change keeps the rest
    for bad in ({"mode": "openai"}, {"pass_score": "11"}, {"budget_usd": "0"}, {"max_rounds": "0"}, {"max_tokens": "5"}):
        assert c.put(f"/api/projects/{pid}", json={"settings": bad}).status_code == 400
    assert c.get(f"/api/projects/{pid}").json()["settings"]["max_rounds"] == 2  # a refused change changed nothing
    decl = c.get("/api/offices").json()["career"]
    assert [a["key"] for a in decl["agents"]] == ["researcher", "writer", "reviewer"] and "mode" in [f["name"] for f in decl["run_settings_fields"]]


# ---- starting a draft ----
def test_nothing_starts_until_the_office_says_the_project_is_ready(c):
    pid = c.post("/api/projects", json={"office": "career", "brief": {"company": "A사", "role": "엔지니어"}}).json()["project"]["id"]
    did = c.post(f"/api/projects/{pid}/deliverables", json={"kind": "answer", "title": "문항"}).json()["deliverables"][0]["id"]
    r = c.post(f"/api/projects/{pid}/deliverables/{did}/draft", json={})
    assert r.status_code == 400 and "근거로 쓸 내 리소스가 없습니다" in r.json()["detail"]
    assert c.get("/api/runs").json() == []
    other, _, _ = ready_project(c)
    assert c.post(f"/api/projects/{other}/deliverables/{did}/draft", json={}).status_code == 404  # a deliverable of another project
    assert c.post(f"/api/projects/{pid}/deliverables/nope/draft", json={}).status_code == 404


def test_a_real_gateway_run_must_be_confirmed_on_purpose(tmp_path):
    client = Script([EVIDENCE])
    c = make(tmp_path, client)
    pid, did, _ = ready_project(c)
    c.put(f"/api/projects/{pid}", json={"settings": {"mode": "hchat"}})
    r = c.post(f"/api/projects/{pid}/deliverables/{did}/draft", json={})
    assert r.status_code == 400 and "confirm_real" in r.json()["detail"] and client.calls == [] and c.get("/api/runs").json() == []
    ok = c.post(f"/api/projects/{pid}/deliverables/{did}/draft", json={"confirm_real": True})
    assert ok.status_code == 200 and ok.json()["mode"] == "hchat"
    wait(c, ok.json()["run_id"])
    cfg = c.get(f"/api/runs/{ok.json()['run_id']}").json()["events"][0]["config"]
    assert cfg["llm"]["mode"] == "hchat" and cfg["llm"]["model"] == "claude-haiku-4-5" and cfg["llm"]["models"] == {"reviewer": "claude-sonnet-5"}  # the settings, snapshotted


def test_a_run_copies_what_it_needs_and_can_be_traced_to_it(c):
    pid, did, rid = ready_project(c)
    run = draft(c, pid, did)
    ev = c.get(f"/api/runs/{run}").json()["events"][0]
    inp = ev["input"]
    assert ev["project_id"] == pid and ev["deliverable_id"] == did
    assert (inp["company"], inp["job"], inp["question"], inp["title"]) == ("A사", "데이터 엔지니어", "지원동기를 서술하시오", "A사 · 데이터 엔지니어")
    assert inp["jd"] == JD and inp["max_chars"] == 500 and inp["count_mode"] == "with_spaces"
    assert [p["name"] for p in inp["evidence"]] == ["내 경험"] and inp["evidence"][0]["text"] == EVIDENCE and inp["evidence_trace"]["all_used"] is True
    assert {r["role"]: r["sha256"] for r in inp["evidence_trace"]["resources"]}["evidence"] == hashlib.sha256(EVIDENCE.encode()).hexdigest()
    assert ev["config"]["rubric"][1]["name"] == "근거일치"  # the project rubric: the material is judged too
    c.put(f"/api/library/resources/{rid}", json={"text": "나중에 고친 내용"})
    assert c.get(f"/api/runs/{run}").json()["events"][0]["input"]["evidence"][0]["text"] == EVIDENCE  # later edits do not reach a past run


def test_only_the_relevant_part_of_a_large_material_is_sent(c):
    filler = "\n\n".join(f"주제{i}에 대한 일반적인 설명 문장입니다. " * 20 for i in range(40))
    big = filler + "\n\n데이터 파이프라인을 설계하고 배치 처리 시간을 단축한 프로젝트 경험이다. Airflow로 스케줄링했다."
    pid, did, _ = ready_project(c, evidence=big, question="데이터 파이프라인 설계 경험을 서술하시오")
    inp = c.get(f"/api/runs/{draft(c, pid, did)}").json()["events"][0]["input"]
    assert inp["evidence_trace"]["all_used"] is False and inp["evidence_trace"]["chars"] <= 6000 < inp["evidence_trace"]["total_chars"]
    assert any("Airflow로 스케줄링" in p["text"] for p in inp["evidence"])


def test_a_withdrawn_sign_off_stops_the_draft(c):
    pid, did, rid = ready_project(c)
    c.put(f"/api/library/resources/{rid}", json={"verified": False})
    r = c.post(f"/api/projects/{pid}/deliverables/{did}/draft", json={})
    assert r.status_code == 400 and "검수 전 자료" in r.json()["detail"]


# ---- the human decides, and an approved result becomes the deliverable's final ----
def test_approval_files_the_result_as_the_final_measured_by_the_office(c):
    pid, did, _ = ready_project(c, spec={"max_chars": 500, "count_mode": "without_spaces"})
    run = draft(c, pid, did)
    assert final_of(c, pid, did)["final_content"] is None  # waiting is not a result
    row = c.get(f"/api/projects/{pid}").json()["runs"][did][0]
    assert row["run_id"] == run and row["status"] == "awaiting_approval" and row["score"] is not None

    c.post(f"/api/runs/{run}/decision", json={"decision": "approve"})
    f = final_of(c, pid, did)
    result = c.get(f"/api/runs/{run}").json()["summary"]["result"]["content"]
    assert f["final_content"] == result and f["final_run_id"] == run and f["final_at"]
    assert f["final_meta"]["count_mode"] == "without_spaces" and f["final_meta"]["max_chars"] == 500 and f["final_meta"]["within_limit"] is True
    assert c.get("/api/projects").json()[0]["finals"] == 1


def test_a_rejection_files_nothing_and_a_revision_files_only_what_is_finally_approved(c):
    pid, did, _ = ready_project(c)
    run = draft(c, pid, did)
    c.post(f"/api/runs/{run}/decision", json={"decision": "reject", "comment": "방향이 다름"})
    assert final_of(c, pid, did)["final_content"] is None

    run = draft(c, pid, did)
    c.post(f"/api/runs/{run}/decision", json={"decision": "revise", "comment": "더 간결하게"})
    assert final_of(c, pid, did)["final_content"] is None  # still not approved
    c.post(f"/api/runs/{run}/decision", json={"decision": "approve"})
    assert final_of(c, pid, did)["final_run_id"] == run


def test_the_office_screen_can_approve_too_and_shows_the_project_title(c):
    pid, did, _ = ready_project(c)
    run = draft(c, pid, did)
    assert c.get("/api/office/runs").json()[0]["job"] == "A사 · 데이터 엔지니어"
    c.post(f"/api/office/runs/{run}/decision", json={"decision": "approve", "comment": ""})
    assert final_of(c, pid, did)["final_content"]


def test_approving_after_the_deliverable_was_deleted_still_approves(c):
    pid, did, _ = ready_project(c)
    run = draft(c, pid, did)
    c.delete(f"/api/deliverables/{did}")
    assert c.post(f"/api/runs/{run}/decision", json={"decision": "approve"}).json()["status"] == "approved"  # nowhere to file it, but the decision stands


def test_working_on_a_question_again_starts_from_the_approved_answer(c):
    pid, did, _ = ready_project(c)
    first = draft(c, pid, did)
    c.post(f"/api/runs/{first}/decision", json={"decision": "approve"})
    approved = final_of(c, pid, did)["final_content"]
    second = draft(c, pid, did)
    assert c.get(f"/api/runs/{second}").json()["events"][0]["input"]["draft"] == approved
    assert [r["run_id"] for r in c.get(f"/api/projects/{pid}").json()["runs"][did]] == [second, first]  # newest first


# ---- the guards through a project run ----
def test_the_project_shows_why_a_high_score_did_not_pass_and_the_human_may_still_decide(tmp_path):
    client = Script(["500GB 로그를 Spark로 처리해 오류율을 93%에서 2%로 낮췄습니다."], score=10)
    c = make(tmp_path, client)
    pid, did, _ = ready_project(c)
    c.put(f"/api/projects/{pid}", json={"settings": {"mode": "hchat", "max_rounds": 1}})
    run = c.post(f"/api/projects/{pid}/deliverables/{did}/draft", json={"confirm_real": True}).json()["run_id"]
    wait(c, run)
    row = c.get(f"/api/projects/{pid}").json()["runs"][did][0]
    assert row["score"] == 10.0 and row["blocked_by"] == ["facts"] and row["stop_reason"] == "max_rounds"
    assert "[코드 검사] 근거 자료에 없는 수치: 500, 93" in next(p for k, p in client.calls if k == "review")
    # the code blocks passing, not the human: the decision is still theirs
    assert c.post(f"/api/runs/{run}/decision", json={"decision": "approve"}).json()["status"] == "approved"


def test_a_saved_prompt_that_hides_the_material_makes_the_run_fail_before_any_call(tmp_path):
    client = Script([EVIDENCE])
    c = make(tmp_path, client)
    pid, did, _ = ready_project(c)
    c.post("/api/library/prompts", json={"workflow": "cover_letter", "role": "writer", "name": "근거없는", "body": "[문항] {{question}}\n{{draft}}\n{{feedback}}"})
    c.put(f"/api/projects/{pid}", json={"settings": {"mode": "hchat", "prompt_writer": "근거없는"}})
    run = c.post(f"/api/projects/{pid}/deliverables/{did}/draft", json={"confirm_real": True}).json()["run_id"]
    wait(c, run, "failed")
    assert "evidence" in c.get(f"/api/runs/{run}").json()["summary"]["error"] and client.calls == []
    assert c.get(f"/api/runs/{run}").json()["events"][0]["config"]["prompt_refs"] == {"writer": "근거없는@1"}
