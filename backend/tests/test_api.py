import time

import pytest
from fastapi.testclient import TestClient

from aiops.api.app import create_app

INPUT = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "저는 데이터에 관심이 많습니다."}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def wait_status(client, rid, want, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/api/runs/{rid}").json()["summary"]
        if s["status"] == want:
            return s
        time.sleep(0.05)
    raise AssertionError(f"status never became {want}: {s['status']}")


def start(client, **overrides):
    r = client.post("/api/runs", json={"workflow": "cover_letter", "input": INPUT, "overrides": overrides})
    assert r.status_code == 200
    return r.json()["run_id"]


def test_page_and_workflows(client):
    assert client.get("/ops").status_code == 200
    assert "cover_letter" in client.get("/api/workflows").json()


def test_start_runs_in_background_then_approve(client):
    rid = start(client)
    s = wait_status(client, rid, "awaiting_approval")
    assert s["stop_reason"] == "passed" and s["totals"]["calls"] == 6
    s = client.post(f"/api/runs/{rid}/decision", json={"decision": "approve", "actor": "boss"}).json()
    assert s["status"] == "approved"
    assert [r["status"] for r in client.get("/api/runs").json()] == ["approved"]


def test_revise_via_api(client):
    rid = start(client, max_rounds=1, llm={"scenario": [5, 9]})
    wait_status(client, rid, "awaiting_approval")
    s = client.post(f"/api/runs/{rid}/decision", json={"decision": "revise", "comment": "구체적으로"}).json()
    assert s["status"] == "awaiting_approval" and s["rounds"] == 2 and s["stop_reason"] == "passed"


def test_error_codes(client):
    rid = start(client)
    wait_status(client, rid, "awaiting_approval")
    assert client.post(f"/api/runs/{rid}/decision", json={"decision": "revise", "comment": ""}).status_code == 400
    assert client.post(f"/api/runs/{rid}/decision", json={"decision": "nope"}).status_code == 400
    client.post(f"/api/runs/{rid}/decision", json={"decision": "reject", "comment": "x"})
    assert client.post(f"/api/runs/{rid}/decision", json={"decision": "approve"}).status_code == 409
    assert client.get("/api/runs/nope").status_code == 404
    assert client.post("/api/runs", json={"workflow": "zzz", "input": {}}).status_code == 404
    assert client.post("/api/runs", json={"workflow": "cover_letter", "input": {}, "overrides": {"llm": {"mode": "real"}}}).status_code == 400
    assert client.post("/api/runs", json={"workflow": "cover_letter", "input": {}, "overrides": {"max_rounds": 0}}).status_code == 400


def test_llm_status_reports_missing_names_never_values(client, monkeypatch):
    monkeypatch.delenv("H_CHAT_API_KEY", raising=False)
    monkeypatch.setenv("H_CHAT_BASE_URL", "https://gateway.example/secret-path")
    s = client.get("/api/llm/status").json()["hchat"]
    assert s == {"configured": False, "missing": ["H_CHAT_API_KEY"]}
    monkeypatch.setenv("H_CHAT_API_KEY", "sk-VERY-SECRET")
    resp = client.get("/api/llm/status")
    assert resp.json()["hchat"]["configured"] is True
    assert "sk-VERY-SECRET" not in resp.text and "secret-path" not in resp.text


def test_llm_test_reports_a_clear_error_when_not_configured(client, monkeypatch):
    for n in ("H_CHAT_API_KEY", "H_CHAT_BASE_URL"):
        monkeypatch.delenv(n, raising=False)
    r = client.post("/api/llm/test", json={"model": "claude-sonnet-5"}).json()
    assert r["ok"] is False and "H_CHAT_API_KEY" in r["error"]


def test_starting_a_real_run_without_env_is_a_400_with_the_reason(client, monkeypatch):
    for n in ("H_CHAT_API_KEY", "H_CHAT_BASE_URL"):
        monkeypatch.delenv(n, raising=False)
    r = client.post("/api/runs", json={"workflow": "cover_letter", "input": INPUT,
                                       "overrides": {"llm": {"mode": "hchat", "model": "claude-sonnet-5"}}})
    assert r.status_code == 400 and "H_CHAT_API_KEY" in r.json()["detail"]
    assert client.get("/api/runs").json() == []  # nothing recorded


def test_rerun_copies_input_and_settings_and_links_the_original(client):
    rid = start(client, pass_score=9.5, max_rounds=2, llm={"scenario": [4, 6]})
    wait_status(client, rid, "awaiting_approval")
    new = client.post(f"/api/runs/{rid}/rerun", json={}).json()["run_id"]
    assert new != rid
    wait_status(client, new, "awaiting_approval")
    a, b = (client.get(f"/api/runs/{x}").json() for x in (rid, new))
    assert b["events"][0]["config"] == a["events"][0]["config"] and b["events"][0]["input"] == a["events"][0]["input"]
    assert b["summary"]["parent"] == rid and a["summary"]["parent"] is None
    assert [s["total"] for s in b["summary"]["scores"]] == [s["total"] for s in a["summary"]["scores"]]  # deterministic fake


def test_rerun_with_overrides_changes_only_those_settings(client):
    rid = start(client, max_rounds=1, llm={"scenario": [5, 9]})
    wait_status(client, rid, "awaiting_approval")
    new = client.post(f"/api/runs/{rid}/rerun", json={"overrides": {"max_rounds": 3, "llm": {"scenario": [9]}}}).json()["run_id"]
    s = wait_status(client, new, "awaiting_approval")
    cfg = client.get(f"/api/runs/{new}").json()["events"][0]["config"]
    assert cfg["max_rounds"] == 3 and cfg["llm"]["scenario"] == [9] and cfg["pass_score"] == 8.0
    assert s["stop_reason"] == "passed" and s["rounds"] == 1


def test_rerun_unknown_run_is_404(client):
    assert client.post("/api/runs/nope/rerun", json={}).status_code == 404
