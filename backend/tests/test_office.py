import json
import time

from fastapi.testclient import TestClient

from aiops.api.app import create_app
from aiops.core.engine import Engine
from aiops.core.store import RunStore
from aiops.llm.fake import FakeLLM
from aiops.office.view import activity
from aiops.workflows import REGISTRY

INPUT = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "저는 데이터에 관심이 많습니다."}


def acts(eng, rid):
    return [a for a in map(activity, eng.store.read(rid)) if a]


def test_every_call_is_announced_before_it_finishes(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY)
    rid = eng.start("cover_letter", INPUT)
    types = [e["type"] for e in eng.store.read(rid) if e["type"] in ("call_started", "llm_call")]
    assert types == ["call_started", "llm_call"] * 6


def test_activity_follows_the_run(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY)
    rid = eng.start("cover_letter", INPUT, {"llm": {"scenario": [5, 9]}, "max_rounds": 1})
    assert acts(eng, rid) == [
        {"k": "begin"},
        {"k": "start", "agent": "writer", "purpose": "write", "round": 1}, {"k": "end", "agent": "writer"},
        {"k": "start", "agent": "reviewer", "purpose": "review", "round": 1}, {"k": "end", "agent": "reviewer"},
        {"k": "gate"},
    ]
    eng.decide(rid, "revise", "더 구체적으로")
    assert {"k": "decision", "decision": "revise"} in acts(eng, rid)
    assert acts(eng, rid)[-1] == {"k": "gate"}  # back to waiting for the human
    eng.decide(rid, "approve")
    assert acts(eng, rid)[-2:] == [{"k": "decision", "decision": "approve"}, {"k": "finished", "status": "approved"}]


def test_failed_run_is_reported(tmp_path):
    class Bad(FakeLLM):
        def _review(self, meta):
            return "not json"

    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda c: Bad(c))
    rid = eng.start("cover_letter", INPUT)
    assert acts(eng, rid)[-1] == {"k": "finished", "status": "failed"}


def test_activity_leaks_no_process_data(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY)
    rid = eng.start("cover_letter", INPUT)
    blob = json.dumps(acts(eng, rid), ensure_ascii=False)
    for secret in ("prompt", "response", "tokens", "cost", INPUT["draft"], "직무적합성"):
        assert secret not in blob


def test_no_score_or_draft_shown_while_running(tmp_path):
    from aiops.core.summary import summarize
    from aiops.office.view import office_view

    eng = Engine(RunStore(tmp_path), REGISTRY)
    ev = eng.store.read(eng.start("cover_letter", INPUT, {"llm": {"scenario": [5, 6]}, "max_rounds": 1}))
    done = office_view(summarize(ev))
    assert done["status"] == "awaiting_approval" and done["score"] and done["result"]

    mid = ev[: next(i for i, e in enumerate(ev) if e["type"] == "score") + 1]  # a score exists, loop not finished
    v = office_view(summarize(mid))
    assert v["status"] == "running" and v["score"] is None and v["result"] is None

    revising = ev + [{"seq": 99, "ts": ev[-1]["ts"], "type": "gate_decision", "decision": "revise", "comment": "x", "actor": "office"}]
    v = office_view(summarize(revising))  # previous draft/score must not look final while a revise pass runs
    assert v["status"] == "running" and v["score"] is None and v["result"] is None


def test_office_api_hides_process_data_and_decides(tmp_path):
    client = TestClient(create_app(tmp_path))
    rid = client.post("/api/runs", json={"workflow": "cover_letter", "input": INPUT}).json()["run_id"]
    end = time.time() + 5
    while client.get(f"/api/office/runs/{rid}").json()["status"] != "awaiting_approval":
        assert time.time() < end
        time.sleep(0.05)
    v = client.get(f"/api/office/runs/{rid}").json()
    assert set(v) == {"run_id", "workflow", "status", "stop_reason", "error", "job", "question", "result", "score", "rounds", "decisions"}
    assert v["score"] == {"total": 9.0, "pass_score": 8.0, "passed": True} and v["result"]
    assert "tokens" not in str(client.get("/api/office/runs").json()) and "cost" not in str(v)

    assert client.post(f"/api/office/runs/{rid}/decision", json={"decision": "revise", "comment": ""}).status_code == 400
    v = client.post(f"/api/office/runs/{rid}/decision", json={"decision": "approve", "actor": "spoofed"}).json()
    assert v["status"] == "approved" and v["decisions"][0]["actor"] == "office"  # actor is fixed server-side
    assert client.post(f"/api/office/runs/{rid}/decision", json={"decision": "approve"}).status_code == 409
    assert client.get("/office").status_code == 200
    assert client.get("/api/office/runs/nope/activity").status_code == 404
