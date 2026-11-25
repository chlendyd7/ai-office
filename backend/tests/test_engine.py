import json

import pytest

from aiops.core.engine import Engine, GateError
from aiops.core.store import RunStore
from aiops.core.summary import summarize
from aiops.llm.fake import FakeLLM
from aiops.workflows import REGISTRY

INPUT = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "저는 데이터에 관심이 많습니다."}


@pytest.fixture
def eng(tmp_path):
    return Engine(RunStore(tmp_path), REGISTRY)


def run(eng, **overrides):
    rid = eng.start("cover_letter", INPUT, overrides or None)
    ev = eng.store.read(rid)
    return rid, ev, summarize(ev)


def calls(ev):
    return [e for e in ev if e["type"] == "llm_call"]


def test_passes_when_score_reaches_threshold(eng):
    _, ev, s = run(eng)  # default scenario 5 -> 7 -> 9, pass 8
    assert [x["total"] for x in s["scores"]] == [5.0, 7.0, 9.0]
    assert s["stop_reason"] == "passed" and s["rounds"] == 3
    assert s["status"] == "awaiting_approval"  # AI never finalizes by itself
    assert s["result"]["content"].startswith(INPUT["draft"]) and s["result"]["round"] == 3


def test_stops_at_max_rounds(eng):
    _, ev, s = run(eng, llm={"scenario": [5.0]}, max_rounds=2)
    assert s["stop_reason"] == "max_rounds" and s["rounds"] == 2
    assert len(calls(ev)) == 4  # 2 x (write + review)
    assert s["status"] == "awaiting_approval"


def test_budget_guard_refuses_a_call_that_could_exceed_the_budget(eng):
    # budget fits the first (write) call but not the worst case of the next (review) call
    _, ev, s = run(eng, budget_usd=0.001, llm={"max_tokens": 16})
    assert s["stop_reason"] == "budget_exceeded"
    assert [c["purpose"] for c in calls(ev)] == ["write"]
    assert s["totals"]["cost_usd"] <= 0.001  # never overshoots
    assert s["status"] == "awaiting_approval"


def test_budget_too_small_for_any_call_sends_nothing(eng):
    _, ev, s = run(eng, budget_usd=0.00001)
    assert s["stop_reason"] == "budget_exceeded" and calls(ev) == []
    assert s["totals"]["cost_usd"] == 0 and s["status"] == "awaiting_approval"


def test_token_and_cost_totals_equal_sum_of_calls(eng):
    _, ev, s = run(eng)
    cs = calls(ev)
    t = s["totals"]
    assert t["calls"] == len(cs) == 6
    assert t["tokens_in"] == sum(c["tokens_in"] for c in cs) > 0
    assert t["tokens_out"] == sum(c["tokens_out"] for c in cs) > 0
    assert t["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in cs))
    for group in (s["by_agent"], s["by_round"]):
        assert sum(b["tokens_in"] for b in group.values()) == t["tokens_in"]
        assert sum(b["tokens_out"] for b in group.values()) == t["tokens_out"]
        assert sum(b["cost_usd"] for b in group.values()) == pytest.approx(t["cost_usd"])
    assert set(s["by_agent"]) == {"writer", "reviewer"}


def test_cost_matches_pricing_config(eng):
    _, ev, _ = run(eng, pricing={"in_per_1k": 1.0, "out_per_1k": 2.0}, budget_usd=100)
    for c in calls(ev):
        assert c["cost_usd"] == pytest.approx(c["tokens_in"] / 1000 * 1.0 + c["tokens_out"] / 1000 * 2.0, abs=1e-6)


def test_config_snapshot_and_full_prompts_recorded(eng):
    _, ev, _ = run(eng, pass_score=9.5, max_rounds=1)
    cfg = ev[0]["config"]
    assert ev[0]["type"] == "run_started" and ev[0]["input"] == INPUT
    assert cfg["pass_score"] == 9.5 and cfg["max_rounds"] == 1 and len(cfg["rubric"]) == 5
    c = calls(ev)
    assert "데이터 엔지니어" in c[0]["prompt"] and c[0]["response"]
    assert "직무적합성" in c[1]["prompt"]
    assert [e["seq"] for e in ev] == list(range(len(ev)))


def test_weighted_total(tmp_path):
    class Uneven(FakeLLM):
        def _review(self, meta):
            return json.dumps({"criteria": [
                {"name": "a", "score": 10, "feedback": "x"}, {"name": "b", "score": 0, "feedback": "y"}]})

    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda c: Uneven(c))
    rubric = [{"name": "a", "weight": 3}, {"name": "b", "weight": 1}]
    _, ev, s = run(eng, rubric=rubric)
    assert s["scores"][0]["total"] == 7.5  # (10*3 + 0*1) / 4


def test_approve_finalizes_and_blocks_further_decisions(eng):
    rid, _, _ = run(eng)
    eng.decide(rid, "approve", "ok", actor="boss")
    s = summarize(eng.store.read(rid))
    assert s["status"] == "approved" and s["decisions"][0]["actor"] == "boss"
    with pytest.raises(GateError):
        eng.decide(rid, "approve")


def test_reject_finalizes(eng):
    rid, _, _ = run(eng)
    eng.decide(rid, "reject", "방향 다름")
    ev = eng.store.read(rid)
    assert summarize(ev)["status"] == "rejected" and ev[-1]["reason"] == "방향 다름"


def test_revise_runs_more_rounds_and_totals_stay_consistent(eng):
    rid, ev0, s0 = run(eng, llm={"scenario": [5.0, 9.0]}, max_rounds=1)
    assert s0["stop_reason"] == "max_rounds" and s0["rounds"] == 1
    eng.decide(rid, "revise", "수치 사례를 더 넣어주세요")
    ev = eng.store.read(rid)
    s = summarize(ev)
    assert s["status"] == "awaiting_approval" and s["stop_reason"] == "passed"
    assert s["rounds"] == 2  # numbering continues
    assert [x["total"] for x in s["scores"]] == [5.0, 9.0]  # fake scenario continues, not restarted
    assert "수치 사례를 더 넣어주세요" in calls(ev)[2]["prompt"]  # human comment reached the writer
    assert s["totals"]["calls"] == 4
    assert s["totals"]["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in calls(ev)))
    assert [d["decision"] for d in s["decisions"]] == ["revise"]
    eng.decide(rid, "approve")
    assert summarize(eng.store.read(rid))["status"] == "approved"


def test_revise_needs_comment_and_valid_decision(eng):
    rid, _, _ = run(eng)
    with pytest.raises(ValueError):
        eng.decide(rid, "revise", "  ")
    with pytest.raises(ValueError):
        eng.decide(rid, "maybe")
    assert summarize(eng.store.read(rid))["status"] == "awaiting_approval"


def test_revise_after_budget_exhausted_makes_no_new_calls(eng):
    rid, ev0, _ = run(eng, budget_usd=0.00001)
    n = len(calls(ev0))
    eng.decide(rid, "revise", "다시")
    ev = eng.store.read(rid)
    assert len(calls(ev)) == n
    assert summarize(ev)["status"] == "awaiting_approval" and summarize(ev)["stop_reason"] == "budget_exceeded"


def test_failure_is_recorded_not_swallowed(tmp_path):
    class Bad(FakeLLM):
        def _review(self, meta):
            return "not json"

    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda c: Bad(c))
    _, ev, s = run(eng)
    assert s["status"] == "failed" and "no JSON object" in s["error"]
    assert len(calls(ev)) == 2  # tokens spent before the failure are still counted
    assert s["totals"]["calls"] == 2


def test_unsupported_llm_mode_is_rejected_before_anything_is_recorded(eng):
    with pytest.raises(ValueError, match="unsupported llm.mode"):
        eng.start("cover_letter", INPUT, {"llm": {"mode": "real"}})
    assert eng.store.list_runs() == []


def test_run_id_is_validated(tmp_path):
    with pytest.raises(ValueError):
        RunStore(tmp_path).read("../etc/passwd")
