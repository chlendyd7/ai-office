import time

import pytest
from fastapi.testclient import TestClient

from aiops.api.app import create_app
from aiops.core.engine import Engine
from aiops.core.jobqueue import JobQueue, Worker
from aiops.core.store import RunStore
from aiops.core.summary import derive_status, summarize
from aiops.llm.fake import FakeLLM
from aiops.workflows import REGISTRY


def job(i=1, **overrides):
    return {"workflow": "cover_letter", "input": {"job": f"직무{i}", "question": "q", "draft": "d"}, "overrides": overrides}


@pytest.fixture
def rig(tmp_path):
    eng = Engine(RunStore(tmp_path / "runs"), REGISTRY)
    q = JobQueue(tmp_path / "q.jsonl")
    return eng, q, Worker(eng, q, background=False)  # synchronous: tick() runs the loop to the approval gate


def statuses(eng):
    return [derive_status(eng.store.read(r)) for r in eng.store.list_runs()]


def test_stopped_by_default_even_with_queued_jobs(rig):
    eng, q, w = rig
    q.enqueue([job()])
    assert w.tick() == "paused" and eng.store.list_runs() == []


def test_runs_jobs_back_to_back_without_a_human(rig):
    eng, q, w = rig
    q.enqueue([job(1), job(2), job(3)])
    w.start(max_pending=10)
    assert [w.tick().split(":")[0] for _ in range(4)] == ["started", "started", "started", "idle"]
    assert statuses(eng) == ["awaiting_approval"] * 3  # the worker never finalizes anything
    assert [j["status"] for j in q.jobs()] == ["started"] * 3 and all(j["run_id"] for j in q.jobs())
    assert w.status()["reason"] == "idle" and w.status()["pending_approvals"] == 3


def test_backpressure_waits_for_a_human_decision(rig):
    eng, q, w = rig
    q.enqueue([job(1), job(2), job(3)])
    w.start(max_pending=2)
    assert w.tick().startswith("started") and w.tick().startswith("started")
    assert w.tick() == "backpressure" and w.status()["reason"] == "backpressure" and w.status()["queued"] == 1
    eng.decide(eng.store.list_runs()[0], "approve")  # a human catches up -> work resumes by itself
    assert w.tick().startswith("started") and w.status()["queued"] == 0


def test_session_budget_pauses_automatically(rig):
    eng, q, w = rig
    q.enqueue([job(1), job(2), job(3)])
    w.start(budget_usd=0.01, max_pending=10)  # one default fake run costs ~$0.016
    assert w.tick().startswith("started")
    assert w.tick() == "budget" and not w.enabled and w.status()["reason"] == "budget"
    assert w.status()["queued"] == 2 and w.tick() == "paused"
    w.start(budget_usd=1.0, max_pending=10)  # a fresh budget on the next start
    assert w.tick().startswith("started")


def test_consecutive_failures_pause_the_worker(tmp_path):
    class Bad(FakeLLM):
        def _review(self, meta):
            return "not json"

    eng = Engine(RunStore(tmp_path / "runs"), REGISTRY, llm_factory=lambda c: Bad(c))
    q = JobQueue(tmp_path / "q.jsonl")
    w = Worker(eng, q, background=False)
    q.enqueue([job(i) for i in range(4)])
    w.start(max_failures=2, max_pending=10)
    assert w.tick().startswith("started") and w.tick().startswith("started")
    assert statuses(eng) == ["failed", "failed"]
    assert w.tick() == "failures" and not w.enabled and w.status()["queued"] == 2  # stops before burning the rest


def test_restart_comes_back_paused_with_the_queue_intact(rig, tmp_path):
    eng, q, w = rig
    q.enqueue([job(1), job(2)])
    w.start()
    w.tick()
    fresh = Worker(eng, JobQueue(tmp_path / "q.jsonl"), background=False)  # "server restarted"
    assert not fresh.enabled and fresh.tick() == "paused"
    assert [j["status"] for j in fresh.queue.jobs()] == ["started", "queued"]


def test_cancel_one_and_clear_all(rig):
    eng, q, w = rig
    ids = q.enqueue([job(1), job(2), job(3)])
    assert q.cancel(ids[0]) and not q.cancel(ids[0])
    assert q.cancel_all() == 2
    w.start()
    assert w.tick() == "idle" and eng.store.list_runs() == []


def test_a_bad_job_does_not_stall_the_queue(rig):
    eng, q, w = rig
    bad = job(1, llm={"mode": "hchat", "model": "claude-sonnet-5"})  # no env in tests -> cannot start
    q.enqueue([bad, job(2)])
    w.start(max_pending=10)
    assert w.tick() == "start_failed" and w.tick().startswith("started")
    j = q.jobs()
    assert j[0]["status"] == "start_failed" and "H_CHAT_API_KEY" in j[0]["error"] and j[1]["status"] == "started"


def test_concurrency_limit(rig):
    eng, q, w = rig
    q.enqueue([job(1), job(2)])
    w.start(concurrency=1, max_pending=10)
    rid = eng.create("cover_letter", job()["input"])  # a run that is still in progress
    w._session.append(rid)
    assert w.tick() == "busy" and w.status()["queued"] == 2 and w.status()["running"] == 1


def test_invalid_start_settings_are_rejected(rig):
    _, _, w = rig
    for bad in ({"budget_usd": 0}, {"max_pending": 0}, {"concurrency": 9}):
        with pytest.raises(ValueError):
            w.start(**bad)
    assert not w.enabled


def test_interrupted_runs_are_closed_at_startup(rig):
    eng, q, w = rig
    stuck = eng.create("cover_letter", job()["input"])  # "running" with no thread: its process died
    done = eng.start("cover_letter", job()["input"])
    assert eng.recover_interrupted() == [stuck]
    s = summarize(eng.store.read(stuck))
    assert s["status"] == "failed" and "InterruptedError" in s["error"]
    assert derive_status(eng.store.read(done)) == "awaiting_approval"  # untouched
    assert eng.recover_interrupted() == []


# ---- API + the real background thread ----

@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "runs", start_worker=True, worker_interval=0.05)
    yield TestClient(app)
    app.state.worker.shutdown()


def wait(pred, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def test_api_batch_is_validated_all_or_nothing(client):
    r = client.post("/api/queue", json={"jobs": [job(1), job(2, rubric=[{"name": "a", "weight": 1}, {"name": "a", "weight": 1}])]})
    assert r.status_code == 400 and "job 2" in r.json()["detail"]
    assert client.get("/api/queue").json()["jobs"] == []  # job 1 was not queued either
    r = client.post("/api/queue", json={"jobs": [job(1, llm={"mode": "hchat", "model": "m"})]})
    assert r.status_code == 400 and "H_CHAT_API_KEY" in r.json()["detail"]  # missing env is caught before any run


def test_api_continuous_loop_end_to_end(client):
    ids = client.post("/api/queue", json={"jobs": [job(1), job(2), job(3)]}).json()["job_ids"]
    assert len(ids) == 3 and client.get("/api/queue/status").json()["enabled"] is False
    time.sleep(0.3)
    assert client.get("/api/runs").json() == []  # nothing runs until started
    st = client.post("/api/queue/control", json={"action": "start", "budget_usd": 1.0, "max_pending": 10}).json()
    assert st["enabled"] and st["configured"]
    wait(lambda: sum(r["status"] == "awaiting_approval" for r in client.get("/api/runs").json()) == 3)
    q = client.get("/api/queue").json()
    assert [j["status"] for j in q["jobs"]] == ["started"] * 3 and q["control"]["queued"] == 0
    wait(lambda: client.get("/api/queue/status").json()["reason"] == "idle")
    assert client.post("/api/queue/control", json={"action": "pause"}).json()["enabled"] is False


def test_api_cancel_and_bad_control(client):
    ids = client.post("/api/queue", json={"jobs": [job(1), job(2)]}).json()["job_ids"]
    assert client.post(f"/api/queue/{ids[0]}/cancel").status_code == 200
    assert client.post(f"/api/queue/{ids[0]}/cancel").status_code == 404
    assert client.post("/api/queue/clear").json() == {"cancelled": 1}
    assert client.post("/api/queue/control", json={"action": "explode"}).status_code == 400
    assert client.post("/api/queue/control", json={"action": "start", "budget_usd": -1}).status_code == 400


def test_resume_keeps_the_session_and_cannot_dodge_the_cap(rig):
    eng, q, w = rig
    q.enqueue([job(1), job(2), job(3)])
    with pytest.raises(ValueError):  # never started
        w.resume()
    w.start(budget_usd=0.02, max_pending=10)  # room for a bit more than one default fake run (~$0.016 each)
    assert w.tick().startswith("started")
    w.pause()
    spent = w.status()["spent_usd"]
    w.resume()
    assert w.status()["spent_usd"] == spent and w.enabled  # same session: nothing was reset
    assert w.tick().startswith("started")  # 0.0163 < 0.02 -> second job allowed
    assert w.tick() == "budget"  # now over the cap
    with pytest.raises(ValueError):
        w.resume()  # a budget stop needs an explicit start with a budget
    w.start(budget_usd=1.0, max_pending=10)
    assert w.tick().startswith("started")


def test_api_resume_is_refused_after_a_budget_stop(client):
    client.post("/api/queue", json={"jobs": [job(1), job(2)]})
    assert client.post("/api/queue/control", json={"action": "resume"}).status_code == 409  # never started
    client.post("/api/queue/control", json={"action": "start", "budget_usd": 0.01, "max_pending": 10})
    wait(lambda: client.get("/api/queue/status").json()["reason"] == "budget")
    assert client.post("/api/queue/control", json={"action": "resume"}).status_code == 409
    assert client.get("/api/queue/status").json()["queued"] == 1
