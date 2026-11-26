"""The career office: a job application (company + role) whose deliverables are the answers to its cover-letter questions."""
from __future__ import annotations

from ..core.evidence import select_evidence
from ..workflows.base import count_chars
from . import Office

QUESTION_TYPES = ["지원동기", "성장과정", "직무역량", "협업", "도전", "기타"]


def measure_answer(spec: dict, content: str) -> dict:
    """The length of an answer by the question's own counting rule, and whether it fits the limit."""
    mode, limit = spec.get("count_mode", "with_spaces"), spec.get("max_chars")
    n = count_chars(content, mode)
    return {"chars": n, "count_mode": mode, "max_chars": limit, "within_limit": None if not limit else n <= limit}


def check_career(brief: dict, deliverables: list[dict], resources: list[dict]) -> dict:
    blocking, warnings = [], []
    if not deliverables:
        blocking.append("자소서 문항이 없습니다")
    if not any(r["role"] == "evidence" for r in resources):
        blocking.append("근거로 쓸 내 리소스가 없습니다 (이력서·경험 정리를 붙이세요)")
    for r in resources:
        if r["review_status"] != "verified":
            blocking.append(f"검수 전 자료가 붙어 있습니다: {r['name']}")
    if not any(r["role"] == "brief" for r in resources):
        warnings.append("직무 설명이 없습니다 (직무에 맞춘 작성이 약해집니다)")
    for d in deliverables:
        if not d["spec"].get("max_chars"):
            warnings.append(f"글자 수 제한이 없습니다: {d['title'][:24]}")
    return {"blocking": blocking, "warnings": warnings}


SETTINGS_FIELDS = [
    {"name": "mode", "label": "LLM", "type": "select", "options": ["fake", "hchat"], "default": "fake",
     "option_labels": {"fake": "가짜 (무료, 시험용)", "hchat": "실제 게이트웨이 (과금)"}},
    {"name": "model_writer", "label": "작성자 모델", "type": "text", "default": "claude-haiku-4-5"},
    {"name": "model_reviewer", "label": "심사자 모델", "type": "text", "default": "claude-sonnet-5"},
    {"name": "max_tokens", "label": "호출당 최대 출력 토큰", "type": "number", "default": 1500, "min": 16},
    {"name": "pass_score", "label": "통과 점수 (0~10)", "type": "decimal", "default": 8.0, "min": 0, "max": 10},
    {"name": "max_rounds", "label": "라운드 상한 (문항 하나)", "type": "number", "default": 3},
    {"name": "budget_usd", "label": "예산 USD (문항 하나)", "type": "decimal", "default": 0.1, "min": 0.001, "max": 5},
    {"name": "step_mode", "label": "라운드마다 멈춰서 확인 (단계 승인)", "type": "bool", "default": False},
    {"name": "prompt_writer", "label": "작성자 프롬프트 (라이브러리: 이름 또는 이름@버전, 비우면 내장)", "type": "text", "required": False},
    {"name": "prompt_reviewer", "label": "심사자 프롬프트 (라이브러리: 이름 또는 이름@버전, 비우면 내장)", "type": "text", "required": False},
]
AGENTS = [
    {"key": "writer", "label": "작성자", "stage": "drafting", "prompt_role": "writer"},
    {"key": "reviewer", "label": "심사자", "stage": "drafting", "prompt_role": "reviewer"},
]


def run_overrides(s: dict) -> dict:
    """Cleaned settings -> what the cover-letter workflow's RunConfig takes. The default is the free fake LLM: spending money is a choice."""
    from ..workflows.cover_letter import PROJECT_RUBRIC

    llm = {"mode": s["mode"], "max_tokens": s["max_tokens"]}
    if s["mode"] == "hchat":
        llm.update(model=s["model_writer"], models={"reviewer": s["model_reviewer"]})
    return {"llm": llm, "pass_score": s["pass_score"], "max_rounds": s["max_rounds"], "budget_usd": s["budget_usd"],
            "step_mode": s["step_mode"], "rubric": PROJECT_RUBRIC}


EVIDENCE_CHARS = 6000  # how much of the user's material one prompt carries


def build_input(project: dict, d: dict, snap: dict) -> dict:
    """The input of one cover-letter run: the question, who and what it is for, the job description, and the passages of the user's
    verified material that overlap most with the question. Everything is copied (text, hashes): the run no longer depends on the library."""
    spec, brief = d["spec"], project["brief"]
    by_role = lambda role: [r for r in snap["resources"] if r["role"] == role]
    jd = "\n\n".join([r["text"] for r in by_role("brief")] + [f"[참고: {r['name']}]\n{r['text']}" for r in by_role("reference")])
    sel = select_evidence(by_role("evidence"), "\n".join([d["title"], spec.get("guidance", ""), jd[:2000]]), EVIDENCE_CHARS)
    inp = {"title": project["title"], "job": brief.get("role", ""), "company": brief.get("company", ""), "question": d["title"],
           "draft": d["final_content"] or "",  # an approved answer is the starting point when the question is worked on again
           "count_mode": spec.get("count_mode", "with_spaces"), "guidance": spec.get("guidance", ""), "qtype": spec.get("qtype", ""),
           "jd": jd, "evidence": sel["passages"],
           "evidence_trace": {"chars": sel["chars"], "total_chars": sel["total_chars"], "all_used": sel["all_used"],
                              "resources": [{"id": r["id"], "name": r["name"], "role": r["role"], "sha256": r["sha256"]} for r in snap["resources"]]}}
    if spec.get("max_chars"):
        inp["max_chars"] = spec["max_chars"]
    return inp


CAREER = Office(
    key="career",
    label="취업 오피스",
    brief_fields=[
        {"name": "company", "label": "회사", "type": "text", "required": True},
        {"name": "role", "label": "직무", "type": "text", "required": True},
        {"name": "source_url", "label": "공고 링크 (참고용, 자동으로 읽지 않음)", "type": "text", "required": False},
    ],
    resource_roles=["evidence", "brief", "reference"],  # my material | the job description | background
    role_labels={"evidence": "근거 (내 자료)", "brief": "직무 설명", "reference": "참고"},
    default_library_role="evidence",
    default_own_role="brief",
    deliverable_kinds={
        "answer": {
            "label": "자소서 문항",
            "workflow": "cover_letter",
            "spec_fields": [
                {"name": "max_chars", "label": "글자 수 제한", "type": "number", "required": False},
                {"name": "count_mode", "label": "글자 수 계산", "type": "select", "options": ["with_spaces", "without_spaces"], "default": "with_spaces",
                 "option_labels": {"with_spaces": "공백 포함", "without_spaces": "공백 제외"}},
                {"name": "qtype", "label": "문항 유형", "type": "select", "options": QUESTION_TYPES, "default": "기타"},
                {"name": "guidance", "label": "작성 지침 (강조할 점)", "type": "textarea", "required": False},
            ],
            "measure": measure_answer,
        },
    },
    stages=["jd_analysis", "company_research", "matching", "drafting", "consistency", "final_report"],
    title_fields=("company", "role"),
    checker=check_career,
    agents=AGENTS,
    run_settings_fields=SETTINGS_FIELDS,
    run_overrides=run_overrides,
    build_input=build_input,
)
