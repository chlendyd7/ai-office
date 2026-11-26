import random

from aiops.core.evidence import MAX_PASSAGE_CHARS, format_evidence, passages_of, select_evidence
from aiops.core.factcheck import describe, find_unsupported

EVIDENCE = ["프로젝트: 로그 수집 파이프라인 구축. 배치 처리 시간을 8시간에서 2시간으로 단축했다. Python과 SQL, Airflow를 썼다.",
            "동아리 회장으로 팀원 12명을 이끌었다."]


# ---- the fact guard ----
def test_figures_and_tools_that_the_material_does_not_contain_are_reported():
    draft = "500GB 규모 로그를 Spark로 처리해 시간을 8시간에서 2시간으로 줄였고 오류율을 93%에서 2%로 낮췄습니다. SQL과 Python도 사용했습니다."
    found = find_unsupported(draft, EVIDENCE)
    assert found == {"numbers": ["500", "93"], "terms": ["GB", "Spark"]}  # 8, 2, SQL, Python are in the material; 2% is not new
    assert "근거 자료에 없는 수치: 500, 93" in describe(found) and "Spark" in describe(found)


def test_what_the_material_says_is_not_flagged_whatever_its_form():
    assert find_unsupported("처리 시간을 8시간에서 2시간으로 줄였습니다. python과 sql을 썼습니다.", EVIDENCE) == {"numbers": [], "terms": []}  # case does not matter
    assert find_unsupported("팀원 12명", ["동아리 회장으로 팀원 12명을 이끌었다"]) == {"numbers": [], "terms": []}
    assert find_unsupported("매출 1,200억", ["연 매출 1200억 규모"]) == {"numbers": [], "terms": []}  # a thousands separator is not a different number
    assert find_unsupported("성장률 3.5%", ["성장률은 3.5% 였다"]) == {"numbers": [], "terms": []}
    assert find_unsupported("성장률 3.5%", ["성장률은 3.50% 였다"])["numbers"] == ["3.5"]  # a different figure is a different figure


def test_blanks_numbering_and_common_words_are_not_claims():
    draft = "1. 지원동기\n2) 성장과정\n성과는 [수치 입력 필요] 입니다. [Spark 경험 입력 필요] and the 팀."
    assert find_unsupported(draft, []) == {"numbers": [], "terms": []}
    assert find_unsupported("A사에 지원합니다", [])["terms"] == []  # one letter is not a term


def test_every_allowed_source_counts_and_nothing_else_does():
    assert find_unsupported("AWS 경험 3년", ["회사: AWS 파트너", "직무 설명: 경력 3년 이상"]) == {"numbers": [], "terms": []}
    assert find_unsupported("Kubernetes", ["Docker"])["terms"] == ["Kubernetes"]
    assert find_unsupported("한글만 있는 문장입니다", []) == {"numbers": [], "terms": []}
    assert describe({"numbers": [], "terms": []}) == ""


# ---- choosing passages ----
def test_passages_follow_paragraphs_headings_and_a_length_limit():
    text = "# 프로젝트 A\n내용 하나\n\n# 프로젝트 B\n내용 둘\n\n" + "문장입니다. " * 400
    ps = passages_of(text)
    assert ps[0] == "# 프로젝트 A\n내용 하나" and ps[1] == "# 프로젝트 B\n내용 둘"
    assert len(ps) > 3 and all(len(p) <= MAX_PASSAGE_CHARS + 20 for p in ps)
    assert passages_of("  \n\n ") == []
    assert passages_of("첫째\n# 제목\n둘째") == ["첫째", "# 제목\n둘째"]  # a heading in the middle of a block opens a new passage


def test_everything_is_used_when_it_fits():
    r = select_evidence([{"id": "r1", "name": "경력", "text": EVIDENCE[0]}, {"id": "r2", "name": "활동", "text": EVIDENCE[1]}], "지원동기", max_chars=6000)
    assert r["all_used"] is True and [p["name"] for p in r["passages"]] == ["경력", "활동"] and r["chars"] == r["total_chars"]


def test_the_passages_that_match_the_question_are_kept_and_the_order_is_preserved():
    filler = [f"주제{i}에 대한 일반적인 설명 문장입니다. " * 20 for i in range(30)]
    target = "데이터 파이프라인을 설계하고 배치 처리 시간을 단축한 프로젝트 경험이다. " * 5
    text = "\n\n".join(filler[:15] + [target] + filler[15:])
    r = select_evidence([{"id": "r1", "name": "경력", "text": text}], "데이터 파이프라인 설계 경험", max_chars=2000)
    assert r["all_used"] is False and r["chars"] <= 2000 and r["total_chars"] > 2000
    assert any("파이프라인" in p["text"] for p in r["passages"])  # the relevant one made it in
    assert [p["index"] for p in r["passages"]] == sorted(p["index"] for p in r["passages"])  # and the text still reads in its own order
    assert r == select_evidence([{"id": "r1", "name": "경력", "text": text}], "데이터 파이프라인 설계 경험", max_chars=2000)  # deterministic


def test_a_query_with_no_overlap_still_returns_something_within_the_limit():
    rnd = random.Random(2)
    text = "\n\n".join("".join(rnd.choice("가나다라마바사아자차") for _ in range(300)) for _ in range(20))
    r = select_evidence([{"id": "r1", "name": "x", "text": text}], "zzz", max_chars=1000)
    assert 0 < r["chars"] <= 1000 and not r["all_used"]


def test_the_block_says_where_each_passage_came_from():
    block = format_evidence([{"name": "경력", "index": 0, "text": "A"}, {"name": "활동", "index": 2, "text": "B"}])
    assert block == "[자료: 경력 #1]\nA\n\n[자료: 활동 #3]\nB"
