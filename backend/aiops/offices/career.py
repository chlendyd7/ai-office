"""The career office: a job application (company + role) whose deliverables are the answers to its cover-letter questions."""
from __future__ import annotations

from . import Office

QUESTION_TYPES = ["지원동기", "성장과정", "직무역량", "협업", "도전", "기타"]

CAREER = Office(
    key="career",
    label="취업 오피스",
    brief_fields=[
        {"name": "company", "label": "회사", "type": "text", "required": True},
        {"name": "role", "label": "직무", "type": "text", "required": True},
        {"name": "source_url", "label": "공고 링크 (참고용, 자동으로 읽지 않음)", "type": "text", "required": False},
    ],
    resource_roles=["evidence", "brief", "reference"],  # my material | the job description | background
    deliverable_kinds={
        "answer": {
            "label": "자소서 문항",
            "workflow": "cover_letter",
            "spec_fields": [
                {"name": "max_chars", "label": "글자 수 제한", "type": "number", "required": False},
                {"name": "count_mode", "label": "글자 수 계산", "type": "select", "options": ["with_spaces", "without_spaces"], "default": "with_spaces"},
                {"name": "qtype", "label": "문항 유형", "type": "select", "options": QUESTION_TYPES, "default": "기타"},
                {"name": "guidance", "label": "작성 지침 (강조할 점)", "type": "textarea", "required": False},
            ],
        },
    },
    stages=["jd_analysis", "company_research", "matching", "drafting", "consistency", "final_report"],
    title_fields=("company", "role"),
)
