import pytest

from aiops.core.engine import Engine
from aiops.core.store import RunStore
from aiops.workflows import REGISTRY
from aiops.workflows.cover_letter import _length_note, _limit

INPUT = {"job": "데이터 엔지니어", "question": "지원동기", "draft": "저는 데이터에 관심이 많습니다."}


@pytest.fixture
def eng(tmp_path):
    return Engine(RunStore(tmp_path), REGISTRY)


def calls(eng, rid, purpose):
    return [e["prompt"] for e in eng.store.read(rid) if e["type"] == "llm_call" and e["purpose"] == purpose]


def test_writer_is_told_not_to_invent_experience(eng):
    rid = eng.start("cover_letter", INPUT)
    assert "지어내지 마세요" in calls(eng, rid, "write")[0]


def test_length_note_is_measured_by_code():
    assert _length_note({"max_chars": 10}, "가" * 12) == "[실제 글자 수] 12자 (제한 10자, 초과 2자)\n"
    assert "준수" in _length_note({"max_chars": 10}, "가" * 10)
    assert _length_note({}, "abc") == "" and _length_note({"max_chars": 0}, "abc") == ""
    assert _limit({"max_chars": "500"}) == 500 and _limit({"max_chars": "x"}) is None  # API input may be a string


def test_reviewer_sees_the_real_length_and_writer_hears_about_an_overrun(eng):
    rid = eng.start("cover_letter", {**INPUT, "max_chars": 5})  # any draft is longer than 5 characters
    assert "[실제 글자 수]" in calls(eng, rid, "review")[0] and "초과" in calls(eng, rid, "review")[0]
    assert "- 분량: [실제 글자 수]" in calls(eng, rid, "write")[1]  # round 2 feedback carries the code-measured overrun


def test_no_limit_means_no_length_text(eng):
    rid = eng.start("cover_letter", INPUT)
    assert "[실제 글자 수]" not in calls(eng, rid, "review")[0]
    assert all("분량:" not in p for p in calls(eng, rid, "write"))
