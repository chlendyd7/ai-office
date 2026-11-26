import time

import pytest
from fastapi.testclient import TestClient

from aiops.api.app import create_app
from aiops.core.engine import Engine, GateError
from aiops.core.store import RunStore
from aiops.core.summary import summarize
from aiops.office.view import activity, office_view
from aiops.workflows import REGISTRY

INPUT = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "저는 데이터에 관심이 많습니다."}


@pytest.fixture
def eng(tmp_path):
    return Engine(RunStore(tmp_path), REGISTRY)


def state(eng, rid):
    return summarize(eng.store.read(rid))


def writer_prompts(eng, rid):
    return [e["prompt"] for e in eng.store.read(rid) if e["type"] == "llm_call" and e["purpose"] == "write"]


def test_pauses_after_every_round_until_the_loop_passes(eng):
    rid = eng.start("cover_letter", INPUT, {"step_mode": True})  # fake scenario 5 -> 7 -> 9, pass at 8
    s = state(eng, rid)
    assert (s["status"], s["stop_reason"], s["rounds"]) == ("awaiting_approval", "step", 1)
    assert [x["total"] for x in s["scores"]] == [5.0] and s["totals"]["calls"] == 2  # exactly one round of work

    eng.decide(rid, "continue")
    s = state(eng, rid)
    assert (s["status"], s["stop_reason"], s["rounds"]) == ("awaiting_approval", "step", 2)

    eng.decide(rid, "continue")
    s = state(eng, rid)
    assert (s["status"], s["stop_reason"], s["rounds"]) == ("awaiting_approval", "passed", 3)  # passing ends the stepping
    assert [x["total"] for x in s["scores"]] == [5.0, 7.0, 9.0] and s["totals"]["calls"] == 6
    assert [d["decision"] for d in s["decisions"]] == ["continue", "continue"]

    eng.decide(rid, "approve")
    assert state(eng, rid)["status"] == "approved"


def test_writer_gets_the_reviewers_last_feedback_and_the_human_comment(eng):
    rid = eng.start("cover_letter", INPUT, {"step_mode": True})
    eng.decide(rid, "continue", "수치를 넣어주세요")
    p = writer_prompts(eng, rid)[1]
    assert "- 직무적합성 (5.0)" in p and "[사람 피드백] 수치를 넣어주세요" in p  # reviewer feedback + human comment

    eng.decide(rid, "continue")  # no comment: still carries the reviewer's feedback
    p = writer_prompts(eng, rid)[2]
    assert "- 직무적합성 (7.0)" in p and "[사람 피드백]" not in p


def test_approve_or_reject_at_a_step_gate_ends_it_with_the_current_draft(eng):
    rid = eng.start("cover_letter", INPUT, {"step_mode": True})
    eng.decide(rid, "approve", "이 정도면 충분")
    s = state(eng, rid)
    assert s["status"] == "approved" and s["result"]["round"] == 1 and s["totals"]["calls"] == 2

    rid2 = eng.start("cover_letter", INPUT, {"step_mode": True})
    eng.decide(rid2, "reject", "방향이 다름")
    assert state(eng, rid2)["status"] == "rejected"


def test_continue_is_only_for_step_gates(eng):
    rid = eng.start("cover_letter", INPUT)  # normal run: loop ends by itself
    with pytest.raises(GateError, match="step gate"):
        eng.decide(rid, "continue")
    assert state(eng, rid)["status"] == "awaiting_approval" and not state(eng, rid)["decisions"]  # nothing was recorded


def test_step_mode_has_no_per_pass_round_cap(eng):
    rid = eng.start("cover_letter", INPUT, {"step_mode": True, "max_rounds": 1, "llm": {"scenario": [5.0]}})
    for _ in range(3):
        assert state(eng, rid)["stop_reason"] == "step"
        eng.decide(rid, "continue")
    assert state(eng, rid)["rounds"] == 4  # the human, not max_rounds, decides when to stop


def test_budget_still_wins_over_stepping(eng):
    rid = eng.start("cover_letter", INPUT, {"step_mode": True, "budget_usd": 0.001, "llm": {"max_tokens": 16}})
    s = state(eng, rid)
    assert s["stop_reason"] == "budget_exceeded" and s["totals"]["cost_usd"] <= 0.001
    with pytest.raises(GateError):
        eng.decide(rid, "continue")  # nothing left to continue with: approve, reject or revise (which re-checks the budget)


def test_setting_is_part_of_the_snapshot(eng):
    rid = eng.start("cover_letter", INPUT, {"step_mode": True})
    assert eng.store.read(rid)[0]["config"]["step_mode"] is True
    assert eng.store.read(eng.start("cover_letter", INPUT))[0]["config"]["step_mode"] is False


def test_office_sees_the_step_gate_and_the_continue_decision(eng):
    rid = eng.start("cover_letter", INPUT, {"step_mode": True})
    v = office_view(summarize(eng.store.read(rid)))
    assert v["status"] == "awaiting_approval" and v["stop_reason"] == "step" and v["score"]["total"] == 5.0 and v["result"]
    eng.decide(rid, "continue")
    acts = [a for a in map(activity, eng.store.read(rid)) if a]
    assert {"k": "decision", "decision": "continue"} in acts and acts[-1] == {"k": "gate"}


def test_api_continue_from_office_and_refusal_elsewhere(tmp_path):
    client = TestClient(create_app(tmp_path))
    def wait(rid, status="awaiting_approval"):
        end = time.time() + 6
        while client.get(f"/api/office/runs/{rid}").json()["status"] != status:
            assert time.time() < end
            time.sleep(0.05)
    rid = client.post("/api/runs", json={"workflow": "cover_letter", "input": INPUT, "overrides": {"step_mode": True}}).json()["run_id"]
    wait(rid)
    v = client.post(f"/api/office/runs/{rid}/decision", json={"decision": "continue", "comment": ""}).json()
    assert v["status"] == "awaiting_approval" and v["rounds"] == 2 and v["stop_reason"] == "step"
    assert v["decisions"][0] == {**v["decisions"][0], "decision": "continue", "actor": "office"}

    plain = client.post("/api/runs", json={"workflow": "cover_letter", "input": INPUT}).json()["run_id"]
    wait(plain)
    assert client.post(f"/api/runs/{plain}/decision", json={"decision": "continue"}).status_code == 409


def test_revise_at_a_step_gate_also_goes_one_more_round_with_the_comment(eng):
    rid = eng.start("cover_letter", INPUT, {"step_mode": True})
    eng.decide(rid, "revise", "더 구체적으로")
    s = state(eng, rid)
    assert s["rounds"] == 2 and s["stop_reason"] == "step"
    p = writer_prompts(eng, rid)[1]
    assert "더 구체적으로" in p and "- 직무적합성 (5.0)" in p
