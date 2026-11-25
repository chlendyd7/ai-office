import json

import httpx
import pytest

from aiops.core.config import LLMConfig
from aiops.core.engine import Engine
from aiops.core.jsonx import extract_json
from aiops.core.store import RunStore
from aiops.core.summary import summarize
from aiops.llm.hchat import HChatClient, HChatError
from aiops.workflows import REGISTRY

KEY = "sk-test-SECRET-123"
ENV = {"H_CHAT_API_KEY": KEY, "H_CHAT_BASE_URL": "https://gateway.example/claude/messages", "H_CHAT_PROJECT_ID": "proj-1"}
INPUT = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "저는 데이터에 관심이 많습니다."}
RUBRIC = ["직무적합성", "구체성", "논리구성", "가독성", "분량준수"]


def reply(text, tin=100, tout=50, blocks=None):
    return httpx.Response(200, json={"content": blocks or [{"type": "text", "text": text}],
                                     "usage": {"input_tokens": tin, "output_tokens": tout}})


def review_text(score=9):
    return json.dumps({"criteria": [{"name": n, "score": score, "feedback": "ok"} for n in RUBRIC]}, ensure_ascii=False)


def client(handler, env=ENV, **cfg):
    cfg = {"mode": "hchat", "model": "claude-sonnet-5", **cfg}
    return HChatClient(LLMConfig(**cfg), env=env, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def call(c, agent="writer"):
    return c.complete(agent=agent, purpose="write", prompt="hi", meta={})


def test_request_shape_and_response_parsing():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), headers=dict(req.headers), body=json.loads(req.content))
        return reply("답변", 12, 4, blocks=[{"type": "thinking", "thinking": "숨김"}, {"type": "text", "text": "답"}, {"type": "text", "text": "변"}])

    res = call(client(handler, max_tokens=256))
    assert (res.text, res.tokens_in, res.tokens_out, res.model) == ("답변", 12, 4, "claude-sonnet-5")  # thinking block dropped
    assert seen["url"] == ENV["H_CHAT_BASE_URL"]
    assert seen["headers"]["authorization"] == f"Bearer {KEY}" and seen["headers"]["x-project-id"] == "proj-1"
    b = seen["body"]
    assert b["model"] == "claude-sonnet-5" and b["max_tokens"] == 256 and b["stream"] is False
    assert b["thinking"] == {"type": "disabled"} and "temperature" not in b
    assert b["messages"] == [{"role": "user", "content": "hi"}] and b["system"]


def test_auth_prefix_and_project_header_are_configurable():
    seen = {}
    env = {k: v for k, v in ENV.items() if k != "H_CHAT_PROJECT_ID"} | {"H_CHAT_AUTH_PREFIX": ""}
    call(client(lambda req: (seen.update(h=dict(req.headers)), reply("x"))[1], env=env))
    assert seen["h"]["authorization"] == KEY and "x-project-id" not in seen["h"]


def test_per_agent_model_override():
    models = []
    c = client(lambda req: (models.append(json.loads(req.content)["model"]), reply("x"))[1], models={"reviewer": "claude-haiku-4-5"})
    assert call(c, "writer").model == "claude-sonnet-5" and call(c, "reviewer").model == "claude-haiku-4-5"
    assert models == ["claude-sonnet-5", "claude-haiku-4-5"]


def test_retries_5xx_then_succeeds():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(503, text="busy") if n["i"] < 3 else reply("ok")

    res = call(client(handler, max_retries=2))
    assert res.text == "ok" and n["i"] == 3 and res.attempts == 3  # retries are visible, not silent


def test_gives_up_after_max_retries():
    n = {"i": 0}
    with pytest.raises(HChatError, match="gave up after 3 attempts.*429"):
        call(client(lambda req: (n.update(i=n["i"] + 1), httpx.Response(429, text="slow down"))[1], max_retries=2))
    assert n["i"] == 3


def test_timeout_is_retried():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ReadTimeout("timed out")
        return reply("ok")

    assert call(client(handler)).text == "ok" and n["i"] == 2


def test_auth_error_is_not_retried_and_hides_the_key():
    n = {"i": 0}
    with pytest.raises(HChatError) as ei:
        call(client(lambda req: (n.update(i=n["i"] + 1), httpx.Response(401, text="unauthorized"))[1]))
    assert n["i"] == 1 and "H_CHAT_AUTH_PREFIX" in str(ei.value) and KEY not in str(ei.value)


def test_missing_env_names_the_variables_not_the_values():
    with pytest.raises(ValueError) as ei:
        HChatClient(LLMConfig(mode="hchat", model="m"), env={"H_CHAT_API_KEY": KEY})
    assert "H_CHAT_BASE_URL" in str(ei.value) and KEY not in str(ei.value)


def test_model_must_be_explicit():
    with pytest.raises(ValueError, match="llm.model"):
        HChatClient(LLMConfig(mode="hchat"), env=ENV)


def test_response_without_usage_or_text_fails_loudly():
    with pytest.raises(HChatError, match="no usage"):
        call(client(lambda req: httpx.Response(200, json={"content": [{"type": "text", "text": "x"}]})))
    with pytest.raises(HChatError, match="empty text"):
        call(client(lambda req: reply("", blocks=[{"type": "thinking", "thinking": "..."}])))


# ---- end to end through the engine: metering, failure recording, secrecy, budget ----

def engine(tmp_path, handler):
    return Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda cfg: client(handler, **cfg.model_dump(exclude={"scenario", "delay_ms", "mode"})))


REAL = {"llm": {"mode": "hchat", "model": "claude-sonnet-5", "models": {"reviewer": "claude-haiku-4-5"}}}


def test_run_with_chatty_reviewer_is_metered_from_real_usage(tmp_path):
    def handler(req):
        if json.loads(req.content)["model"] == "claude-haiku-4-5":  # reviewer: prose around JSON
            return reply("평가 결과입니다.\n" + review_text(9) + "\n이상입니다.", 700, 120)
        return reply("개선된 자기소개서", 300, 80)

    eng = engine(tmp_path, handler)
    rid = eng.start("cover_letter", INPUT, REAL | {"pricing_models": {"claude-haiku-4-5": {"in_per_1k": 0.001, "out_per_1k": 0.005}}})
    ev = eng.store.read(rid)
    s = summarize(ev)
    assert s["status"] == "awaiting_approval" and s["stop_reason"] == "passed"
    assert (s["totals"]["tokens_in"], s["totals"]["tokens_out"]) == (1000, 200)  # exactly what the "API" reported
    models = [e["model"] for e in ev if e["type"] == "llm_call"]
    assert models == ["claude-sonnet-5", "claude-haiku-4-5"]
    w, r = [e for e in ev if e["type"] == "llm_call"]
    assert w["cost_usd"] == pytest.approx(0.3 * 0.003 + 0.08 * 0.015)  # default price
    assert r["cost_usd"] == pytest.approx(0.7 * 0.001 + 0.12 * 0.005)  # per-model price
    assert KEY not in json.dumps(ev)
    assert [e["attempts"] for e in ev if e["type"] == "llm_call"] == [1, 1]


def test_gateway_failure_is_recorded_and_secret_never_leaks(tmp_path):
    n = {"i": 0}

    def handler(req):  # writer ok, then every later request (the reviewer's) 500s
        n["i"] += 1
        return reply("초안", 300, 80) if n["i"] == 1 else httpx.Response(500, text="boom")

    eng = engine(tmp_path, handler)
    rid = eng.start("cover_letter", INPUT, REAL | {"llm": {**REAL["llm"], "max_retries": 1}})
    ev = eng.store.read(rid)
    s = summarize(ev)
    assert s["status"] == "failed" and s["failed_calls"] == 1 and s["totals"]["calls"] == 1
    failed = next(e for e in ev if e["type"] == "call_failed")
    assert failed["agent"] == "reviewer" and "gave up after 2 attempts" in failed["error"] and failed["prompt"]
    assert KEY not in json.dumps(ev)


def test_budget_guard_sends_nothing_when_a_call_could_overshoot(tmp_path):
    n = {"i": 0}
    eng = engine(tmp_path, lambda req: (n.update(i=n["i"] + 1), reply("x"))[1])
    rid = eng.start("cover_letter", INPUT, REAL | {"budget_usd": 0.001, "llm": {**REAL["llm"], "max_tokens": 4000}})
    s = summarize(eng.store.read(rid))
    assert n["i"] == 0 and s["stop_reason"] == "budget_exceeded" and s["totals"]["calls"] == 0


# ---- json extraction ----

@pytest.mark.parametrize("raw", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '```\n{"a": 1}\n```',
    '앞 설명\n{"a": 1}\n뒤 설명',
    '중괄호 {먼저} 나오고\n{"a": 1}',
    'x {"a": 1} y {"b": 2}',
])
def test_extract_json_variants(raw):
    assert extract_json(raw) == {"a": 1}


def test_extract_json_handles_braces_in_strings_and_fails_clearly():
    assert extract_json('설명 {"a": "}{", "b": {"c": 1}} 끝') == {"a": "}{", "b": {"c": 1}}
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json("죄송합니다. 평가할 수 없습니다.")
