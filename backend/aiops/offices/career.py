"""The career office: a job application (company + role) whose deliverables are the answers to its cover-letter questions."""
from __future__ import annotations

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
)
