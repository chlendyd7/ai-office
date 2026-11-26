import json

import pytest

from aiops.core.engine import Engine
from aiops.core.store import RunStore
from aiops.core.summary import summarize
from aiops.llm.base import LLMResult
from aiops.workflows import REGISTRY
from aiops.workflows.cover_letter import PROJECT_RUBRIC, _reviewer_prompt, _writer_prompt

EVIDENCE = [{"resource_id": "r1", "name": "경력", "index": 0,
             "text": "프로젝트: 로그 수집 파이프라인 구축. 배치 처리 시간을 8시간에서 2시간으로 단축했다. Python과 SQL, Airflow를 썼다."}]
INPUT = {"job": "데이터 엔지니어", "company": "A사", "question": "지원동기를 서술하시오", "draft": "", "jd": "자격요건: SQL, Python 3년 이상",
         "evidence": EVIDENCE, "guidance": "파이프라인 경험 중심", "max_chars": 120, "count_mode": "without_spaces"}
GOOD = "배치 처리 시간을 8시간에서 2시간으로 줄인 경험이 있습니다. Python과 SQL로 파이프라인을 만들었습니다."
INVENTED = "500GB 로그를 Spark로 처리해 오류율을 93%에서 2%로 낮췄습니다."


def review_json(score, rubric=PROJECT_RUBRIC):
    return json.dumps({"criteria": [{"name": r["name"], "score": score, "feedback": f"{r['name']} 피드백"} for r in rubric]}, ensure_ascii=False)


class Script:
    """Answers writer calls from one list and reviewer calls from another, and keeps the prompts it was sent."""

    model = "scripted"

    def __init__(self, writes, reviews):
        self.writes, self.reviews, self.sent = list(writes), list(reviews), []

    def complete(self, *, agent, purpose, prompt, meta, attachments=None):
        self.sent.append((purpose, prompt))
        queue = self.writes if purpose == "write" else self.reviews
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        return LLMResult(text, 100, 50, model="claude-haiku-4-5")

    def prompts(self, purpose):
        return [p for k, p in self.sent if k == purpose]


def run(tmp_path, client, inp=INPUT, **overrides):
    eng = Engine(RunStore(tmp_path), REGISTRY, llm_factory=lambda cfg: client)
    rid = eng.start("cover_letter", inp, {"rubric": PROJECT_RUBRIC, "pass_score": 8.0, "max_rounds": 3, **overrides})
    return eng, rid


def scores(eng, rid):
    return summarize(eng.store.read(rid))["scores"]


# ---- what the models are given ----
def test_a_project_run_gives_the_writer_the_context_and_only_the_material_as_a_source():
    p = _writer_prompt(INPUT, "", "")
    for needle in ("[지원 회사] A사", "[지원 직무] 데이터 엔지니어", "[문항] 지원동기를 서술하시오", "[분량 제한] 120자 이내 (공백 제외)", "[작성 지침] 파이프라인 경험 중심",
                   "[직무 설명]\n자격요건: SQL, Python 3년 이상", "[근거 자료]\n[자료: 경력 #1]\n프로젝트: 로그 수집", "[규칙] [근거 자료]에 적힌", "영문 용어"):
        assert needle in p, needle
    assert "지어내" not in p and "초안과 사람 피드백에 없는" not in p  # the project rule replaces the plain-input rule
    r = _reviewer_prompt(INPUT, GOOD, [], None, "")
    assert "[근거 자료]\n[자료: 경력 #1]" in r and "'근거일치'를 낮게" in r and "[실제 글자 수] " in r and "공백 제외" in r


def test_plain_input_without_material_keeps_the_original_prompts():
    plain = {"job": "J", "question": "Q", "max_chars": 300}
    assert "[근거 자료]" not in _writer_prompt(plain, "D", "F") and "지어내지 마세요" in _writer_prompt(plain, "D", "F")
    assert "[실제 글자 수] 1자 (제한 300자, 준수)" in _reviewer_prompt(plain, "D", [])  # no counting rule given: the wording is what it was


def test_a_custom_writer_prompt_must_still_carry_the_material():
    with pytest.raises(ValueError, match="evidence"):
        _writer_prompt(INPUT, "", "", "[문항] {{question}}\n[현재 초안]\n{{draft}}\n[피드백]\n{{feedback}}")
    assert "[근거 자료]" in _writer_prompt(INPUT, "", "", "{{evidence}}\n{{draft}}\n{{feedback}}")


def test_a_run_whose_prompt_drops_the_material_fails_loudly_and_spends_nothing(tmp_path):
    client = Script([GOOD], [review_json(9)])
    eng, rid = run(tmp_path, client, prompts={"writer": "{{question}}\n{{draft}}\n{{feedback}}"})
    s = summarize(eng.store.read(rid))
    assert s["status"] == "failed" and "evidence" in s["error"] and client.sent == []


# ---- the guards: a high score is not enough ----
def test_invented_figures_block_the_pass_whatever_the_reviewer_scores(tmp_path):
    client = Script([INVENTED, GOOD], [review_json(10)])  # the reviewer is delighted by the invention
    eng, rid = run(tmp_path, client)
    s1, s2 = scores(eng, rid)[:2]
    assert s1["total"] == 10.0 and s1["passed"] is False and s1["blocked_by"] == ["facts"]
    assert s1["checks"]["facts"] == {"numbers": ["500", "93"], "terms": ["GB", "Spark"]}
    assert s2["passed"] is True and s2["blocked_by"] == []
    assert "[코드 검사] 근거 자료에 없는 수치: 500, 93" in client.prompts("review")[0]  # the reviewer is told what the code found
    assert "- 근거 검사: 근거 자료에 없는 수치: 500, 93" in client.prompts("write")[1]  # and so is the writer, for the next round
    assert summarize(eng.store.read(rid))["stop_reason"] == "passed" and summarize(eng.store.read(rid))["rounds"] == 2


def test_repeating_an_invention_does_not_make_it_allowed(tmp_path):
    client = Script([INVENTED, INVENTED, INVENTED], [review_json(10)])
    eng, rid = run(tmp_path, client)
    assert [s["blocked_by"] for s in scores(eng, rid)] == [["facts"], ["facts"], ["facts"]]  # earlier drafts are not a source
    assert summarize(eng.store.read(rid))["stop_reason"] == "max_rounds"


def test_the_reviewers_numbers_are_not_a_source_but_the_humans_words_are(tmp_path):
    client = Script(["처리 시간을 8시간에서 2시간으로 줄였고 팀은 15명이었습니다.", GOOD], [review_json(10)])
    eng, rid = run(tmp_path, client)
    assert scores(eng, rid)[0]["checks"]["facts"]["numbers"] == ["15"]  # 15 appears nowhere the writer may draw on
    eng.decide(rid, "revise", "팀 규모는 15명이라고 써 주세요")  # now a human said it
    client.writes = ["처리 시간을 8시간에서 2시간으로 줄였고 팀은 15명이었습니다."]
    eng.decide(rid, "revise", "다시 확인")  # the first comment is part of the record, not only the latest one
    assert scores(eng, rid)[-1]["checks"]["facts"] == {"numbers": [], "terms": []}


def test_exceeding_the_limit_blocks_the_pass_and_the_counting_rule_is_the_questions_own(tmp_path):
    spaced = "배치 처리 시간을 8시간에서 2시간으로 줄인 경험이 있습니다 Python과 SQL로 만들었습니다 " * 2  # long with spaces, shorter without
    from aiops.workflows.base import count_chars
    with_sp, without_sp = count_chars(spaced, "with_spaces"), count_chars(spaced, "without_spaces")
    assert with_sp > without_sp + 10 and count_chars(GOOD, "with_spaces") < (with_sp + without_sp) // 2
    inp = {**INPUT, "max_chars": (with_sp + without_sp) // 2}  # a limit between the two ways of counting

    client = Script([spaced], [review_json(10)])
    eng, rid = run(tmp_path / "a", client, inp={**inp, "count_mode": "without_spaces"}, max_rounds=1)
    assert scores(eng, rid)[0]["passed"] is True and scores(eng, rid)[0]["checks"]["over"] == 0

    client = Script([spaced, GOOD], [review_json(10)])
    eng, rid = run(tmp_path / "b", client, inp={**inp, "count_mode": "with_spaces"})
    s1 = scores(eng, rid)[0]
    assert s1["passed"] is False and s1["blocked_by"] == ["length"] and s1["checks"]["over"] > 0
    assert "- 분량: [실제 글자 수]" in client.prompts("write")[1] and "공백 포함" in client.prompts("write")[1]


def test_a_plain_run_is_unchanged_except_that_a_limit_now_blocks_the_pass(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY)  # the fake LLM
    rid = eng.start("cover_letter", {"job": "J", "question": "Q", "draft": "초안"})
    s = scores(eng, rid)
    assert [x["passed"] for x in s] == [False, False, True] and all(x["blocked_by"] == [] and x["checks"] is None for x in s)
    rid2 = eng.start("cover_letter", {"job": "J", "question": "Q", "draft": "초안", "max_chars": 3})
    assert all(x["blocked_by"] == ["length"] for x in scores(eng, rid2)) and summarize(eng.store.read(rid2))["stop_reason"] == "max_rounds"


def test_a_grounded_run_with_the_fake_llm_passes_without_inventing_anything(tmp_path):
    eng = Engine(RunStore(tmp_path), REGISTRY)
    rid = eng.start("cover_letter", {**INPUT, "max_chars": 500}, {"rubric": PROJECT_RUBRIC})  # fake scenario 5 -> 7 -> 9
    s = summarize(eng.store.read(rid))
    assert s["stop_reason"] == "passed" and [x["total"] for x in s["scores"]] == [5.0, 7.0, 9.0]
    assert all(x["checks"]["facts"] == {"numbers": [], "terms": []} for x in s["scores"])


def test_the_evidence_actually_sent_is_in_the_run_record(tmp_path):
    client = Script([GOOD], [review_json(9)])
    eng, rid = run(tmp_path, client)
    ev = eng.store.read(rid)
    assert ev[0]["input"]["evidence"][0]["name"] == "경력" and "8시간" in ev[0]["input"]["evidence"][0]["text"]  # traceable: what the writer was given
    assert "[자료: 경력 #1]" in next(e for e in ev if e["type"] == "llm_call")["prompt"]
