import json

import pytest
from fastapi.testclient import TestClient

from aiops.api.app import PRICING_FILE, create_app
from aiops.core.engine import Engine
from aiops.core.pricing import load_prices
from aiops.core.store import RunStore
from aiops.core.summary import summarize
from aiops.llm.base import LLMResult
from aiops.workflows import REGISTRY

INPUT = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "초안"}
RUBRIC = ["직무적합성", "구체성", "논리구성", "가독성", "분량준수"]


class Scripted:
    """Reports exactly the usage seen on the gateway dashboard (same tokens, same models)."""
    model = "claude-sonnet-5"

    def complete(self, *, agent, purpose, prompt, meta):
        if purpose == "write":
            return LLMResult("개선본", 1272, 550, "claude-sonnet-5")
        txt = json.dumps({"criteria": [{"name": n, "score": 9, "feedback": "ok"} for n in RUBRIC]}, ensure_ascii=False)
        return LLMResult(txt, 14, 4, "claude-haiku-4-5")


def test_load_prices_converts_per_million_to_per_thousand(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"models": {"m": {"in_per_1m": 3.0, "out_per_1m": 15.0}}}))
    assert load_prices(f) == {"m": {"in_per_1k": 0.003, "out_per_1k": 0.015}}
    assert load_prices(tmp_path / "missing.json") == {}


def test_costs_reproduce_the_gateway_dashboard_exactly(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda c: Scripted(), prices=load_prices(PRICING_FILE))
    rid = eng.start("cover_letter", INPUT)
    ev = eng.store.read(rid)
    w, r = [e for e in ev if e["type"] == "llm_call"]
    assert w["cost_usd"] == pytest.approx(0.008044, abs=1e-9)  # dashboard: claude-sonnet-5, 1272 in / 550 out
    assert r["cost_usd"] == pytest.approx(0.000034, abs=1e-9)  # dashboard: claude-haiku-4-5, 14 in / 4 out
    bm = summarize(ev)["by_model"]
    assert bm["claude-sonnet-5"]["tokens_in"] == 1272 and bm["claude-haiku-4-5"]["tokens_out"] == 4
    assert sum(b["cost_usd"] for b in bm.values()) == pytest.approx(summarize(ev)["totals"]["cost_usd"])


def test_table_is_part_of_the_config_snapshot_and_requests_override_it(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda c: Scripted(), prices=load_prices(PRICING_FILE))
    rid = eng.start("cover_letter", INPUT, {"pricing_models": {"claude-haiku-4-5": {"in_per_1k": 0.004, "out_per_1k": 0.02}}})
    ev = eng.store.read(rid)
    pm = ev[0]["config"]["pricing_models"]
    assert pm["claude-haiku-4-5"]["in_per_1k"] == 0.004  # request wins over the table
    assert pm["claude-sonnet-5"] == {"in_per_1k": 0.002, "out_per_1k": 0.01}  # table fills the rest
    haiku = [e for e in ev if e["type"] == "llm_call"][1]
    assert haiku["cost_usd"] == pytest.approx(14 / 1000 * 0.004 + 4 / 1000 * 0.02)


def test_engine_without_a_table_uses_the_default_estimate(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda c: Scripted())
    ev = eng.store.read(eng.start("cover_letter", INPUT))
    w = next(e for e in ev if e["type"] == "llm_call")
    assert w["cost_usd"] == pytest.approx(1.272 * 0.003 + 0.55 * 0.015)


def test_pricing_endpoint_lists_verified_models(tmp_path):
    r = TestClient(create_app(tmp_path)).get("/api/pricing").json()
    assert {"claude-haiku-4-5", "claude-sonnet-5"} <= set(r["models"])
    assert r["models"]["claude-haiku-4-5"] == {"in_per_1m": 1.0, "out_per_1m": 5.0}
