"""Cover-letter (자기소개서) writer <-> reviewer loop. First workload on the AI Ops core."""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..core.evidence import format_evidence
from ..core.factcheck import describe, find_unsupported
from ..core.jsonx import extract_json
from ..core.metered import BudgetExceeded
from .base import BUDGET_EXCEEDED, MAX_ROUNDS, PASSED, STEP, PassContext, check_template, clean_fields, count_chars, render

DEFAULT_RUBRIC = [
    {"name": "직무적합성", "weight": 0.30, "description": "지원 직무에 필요한 역량·경험과 직접 연결되는가"},
    {"name": "구체성", "weight": 0.25, "description": "수치·사례·본인 역할이 구체적으로 드러나는가"},
    {"name": "논리구성", "weight": 0.20, "description": "문항 의도에 맞고 두괄식/인과 흐름이 자연스러운가"},
    {"name": "가독성", "weight": 0.15, "description": "문장이 간결하고 중복·군더더기가 없는가"},
    {"name": "분량준수", "weight": 0.10, "description": "분량 제한을 지키면서 알찬가"},
]


class _S(TypedDict):
    round: int
    in_pass: int
    review_idx: int
    draft: str
    feedback: str
    passed: bool
    stop_reason: str


def _limit(inp: dict) -> int | None:
    try:
        return int(inp["max_chars"]) or None
    except (KeyError, TypeError, ValueError):
        return None


MODE_LABEL = {"with_spaces": "공백 포함", "without_spaces": "공백 제외"}


def _count(inp: dict, text: str) -> int:
    """The question's own counting rule (a project question says whether spaces count); plain input counts everything."""
    return count_chars(text, inp.get("count_mode") or "with_spaces")


def _over(inp: dict, text: str) -> int:
    """How many characters beyond the limit (0 = within it, or no limit)."""
    lim = _limit(inp)
    return max(0, _count(inp, text) - lim) if lim else 0


def _length_note(inp: dict, draft: str) -> str:
    """Measured by code: an LLM reviewer cannot count characters, and a limit is a fact, not an opinion."""
    lim = _limit(inp)
    if not lim:
        return ""
    n, over = _count(inp, draft), _over(inp, draft)
    label = f", {MODE_LABEL[inp['count_mode']]}" if inp.get("count_mode") in MODE_LABEL else ""
    return f"[실제 글자 수] {n}자 (제한 {lim}자{label}, {f'초과 {over}자' if over else '준수'})\n"


WRITER_TEMPLATE = (
    "당신은 자기소개서 작성 전문가입니다. 아래 문항에 맞게 자기소개서를 개선하세요.\n"
    "[지원 직무] {{job}}\n[문항] {{question}}\n{{limit}}[현재 초안]\n{{draft}}\n[피드백]\n{{feedback}}\n"
)
# Fixed in code, not editable: the guard against invented experience, and the output instruction.
WRITER_FIXED = (
    "[규칙] 지원자가 제공한 초안과 사람 피드백에 없는 경험·수치·기술·성과를 지어내지 마세요. "
    "구체화가 필요하지만 근거가 없으면 [수치 입력 필요]처럼 빈칸으로 표시하세요.\n"
    "개선된 자기소개서 본문만 출력하세요."
)
REVIEWER_TEMPLATE = (
    "당신은 엄격한 자기소개서 심사위원입니다. 루브릭 각 항목을 0~10점으로 채점하고, "
    "항목별로 개선 방향을 한 문장으로 적어 JSON으로만 출력하세요.\n"
    "[지원 직무] {{job}}\n[문항] {{question}}\n[루브릭]\n{{rubric}}\n[자기소개서]\n{{draft}}\n"
)
# Fixed in code: the code-measured length line, and the JSON shape the parser depends on.
REVIEWER_FORMAT = '출력 형식: {"criteria":[{"name":"항목명","score":0-10,"feedback":"..."}]}'
REVIEWER_FIXED = "(코드가 측정한 [실제 글자 수] 줄이 자동으로 붙음)\n" + REVIEWER_FORMAT

# A run started from a project question also carries the company, the job description, the user's verified material (passages) and
# the question's guidance. The writer may use only that material; a code check (core/factcheck) looks for invented figures and terms.
WRITER_TEMPLATE_EV = (
    "당신은 자기소개서 작성 전문가입니다. 아래 문항에 맞게 자기소개서를 작성(개선)하세요.\n"
    "[지원 회사] {{company}}\n[지원 직무] {{job}}\n[문항] {{question}}\n{{limit}}{{guidance}}"
    "[직무 설명]\n{{jd}}\n\n[근거 자료]\n{{evidence}}\n\n[현재 초안]\n{{draft}}\n[피드백]\n{{feedback}}\n"
)
WRITER_FIXED_EV = (
    "[규칙] [근거 자료]에 적힌 경험·수치·기술·성과만 사용하세요. 근거 자료에 없는 숫자나 영문 용어(도구·기술 이름)를 만들어 넣지 마세요. "
    "직무 설명이 요구하는 것에 맞춰 근거 자료의 사실을 골라 연결하되, 구체화에 필요한 근거가 없으면 [입력 필요]처럼 빈칸으로 두세요.\n"
    "개선된 자기소개서 본문만 출력하세요."
)
REVIEWER_TEMPLATE_EV = (
    "당신은 엄격한 자기소개서 심사위원입니다. 루브릭 각 항목을 0~10점으로 채점하고, "
    "항목별로 개선 방향을 한 문장으로 적어 JSON으로만 출력하세요. 자기소개서의 모든 사실은 [근거 자료]에 있어야 하며, "
    "없는 경험·수치·기술이 있으면 '근거일치'를 낮게 주세요.\n"
    "[지원 회사] {{company}}\n[지원 직무] {{job}}\n[문항] {{question}}\n[직무 설명]\n{{jd}}\n\n[근거 자료]\n{{evidence}}\n\n"
    "[루브릭]\n{{rubric}}\n[자기소개서]\n{{draft}}\n"
)
PROJECT_RUBRIC = [
    {"name": "직무적합성", "weight": 0.25, "description": "직무 설명이 요구하는 역량·경험과 직접 연결되는가"},
    {"name": "근거일치", "weight": 0.25, "description": "모든 사실이 [근거 자료]에 있는가 (없는 수치·경험·기술이 있으면 0점)"},
    {"name": "구체성", "weight": 0.20, "description": "근거 자료의 사례·수치·본인 역할이 구체적으로 드러나는가"},
    {"name": "논리구성", "weight": 0.15, "description": "문항 의도에 맞고 두괄식/인과 흐름이 자연스러운가"},
    {"name": "가독성", "weight": 0.10, "description": "문장이 간결하고 중복·군더더기가 없는가"},
    {"name": "분량준수", "weight": 0.05, "description": "분량 제한을 지키면서 알찬가 (코드가 잰 글자 수를 따른다)"},
]

INPUT_FIELDS = [
    {"name": "job", "label": "지원 직무", "type": "text", "required": True},
    {"name": "question", "label": "문항", "type": "textarea", "required": True},
    {"name": "draft", "label": "초안 (비우면 처음부터 작성)", "type": "textarea", "required": False},
    {"name": "max_chars", "label": "분량 제한(자)", "type": "number", "required": False},
]
PROMPT_ROLES = {
    "writer": {"label": "작성자 (writer)", "default": WRITER_TEMPLATE, "default_grounded": WRITER_TEMPLATE_EV,
               "vars": ["job", "question", "limit", "draft", "feedback", "company", "jd", "evidence", "guidance"],
               "required": ["draft", "feedback"], "fixed": WRITER_FIXED, "fixed_grounded": WRITER_FIXED_EV},
    "reviewer": {"label": "심사자 (reviewer)", "default": REVIEWER_TEMPLATE, "default_grounded": REVIEWER_TEMPLATE_EV,
                 "vars": ["job", "question", "rubric", "draft", "company", "jd", "evidence"],
                 "required": ["draft", "rubric"], "fixed": REVIEWER_FIXED},
}


def _grounded(inp: dict) -> bool:
    return bool(inp.get("evidence"))


def _context_values(inp: dict) -> dict:
    return {"company": inp.get("company", ""), "jd": inp.get("jd") or "(직무 설명 없음)",
            "evidence": format_evidence(inp["evidence"]) if _grounded(inp) else "", "guidance": f"[작성 지침] {inp['guidance']}\n" if inp.get("guidance") else ""}


def _writer_prompt(inp: dict, draft: str, feedback: str, template: str | None = None) -> str:
    grounded = _grounded(inp)
    template = template or (WRITER_TEMPLATE_EV if grounded else WRITER_TEMPLATE)
    if grounded and "{{evidence}}" not in template:  # the writer would never see the material the guard allows
        raise ValueError("이 프롬프트에는 {{evidence}}가 없어 근거 자료가 작성자에게 전달되지 않습니다")
    mode = f" ({MODE_LABEL[inp['count_mode']]})" if inp.get("count_mode") in MODE_LABEL else ""
    values = {"job": inp.get("job", ""), "question": inp.get("question", ""), "draft": draft,
              "limit": f"[분량 제한] {_limit(inp)}자 이내{mode}\n" if _limit(inp) else "", "feedback": feedback or "없음 (첫 작성)", **_context_values(inp)}
    return render(template, values).rstrip("\n") + "\n" + (WRITER_FIXED_EV if grounded else WRITER_FIXED)


def _reviewer_prompt(inp: dict, draft: str, rubric, template: str | None = None, extra_note: str = "") -> str:
    grounded = _grounded(inp)
    rb = "\n".join(f"- {c.name} (가중치 {c.weight}): {c.description}" for c in rubric)
    values = {"job": inp.get("job", ""), "question": inp.get("question", ""), "rubric": rb, "draft": draft, **_context_values(inp)}
    return (render(template or (REVIEWER_TEMPLATE_EV if grounded else REVIEWER_TEMPLATE), values).rstrip("\n") + "\n"
            + _length_note(inp, draft) + extra_note + REVIEWER_FORMAT)


def _allowed_texts(inp: dict, human_comments: list[str]) -> list[str]:
    """What a draft may draw its facts from: the material the writer was shown, the job description, who and what is applied for,
    the question and its guidance, the applicant's own starting draft and what the human has said. NOT the reviewer's feedback and
    NOT earlier drafts of the model (an invented figure must not become 'allowed' by being repeated)."""
    return ["\n".join(p["text"] for p in inp.get("evidence", [])), inp.get("jd", ""), inp.get("company", ""), inp.get("job", ""),
            inp.get("question", ""), inp.get("guidance", ""), inp.get("draft", ""), *human_comments]


def _parse_review(raw: str, rubric) -> list[dict]:
    by = {c["name"]: c for c in extract_json(raw)["criteria"]}
    out = []
    for c in rubric:
        if c.name not in by:
            raise ValueError(f"reviewer response missing criterion: {c.name}")
        score = min(10.0, max(0.0, float(by[c.name]["score"])))
        out.append({"name": c.name, "weight": c.weight, "score": score, "feedback": str(by[c.name].get("feedback", ""))})
    return out


def _reviewer_feedback(criteria: list[dict]) -> str:
    return "\n".join(f"- {c['name']} ({c['score']}): {c['feedback']}" for c in criteria)


def _opening_feedback(ctx: PassContext) -> str:
    """What the writer is told at the start of a pass. After a step gate the loop simply goes on, so the writer also gets
    the reviewer's last feedback (a plain human revise starts from the human's comment alone, as before)."""
    human = (ctx.feedback or "").strip()
    last_stop = next((e["reason"] for e in reversed(ctx.events) if e["type"] == "loop_stopped"), None)
    last_score = next((e for e in reversed(ctx.events) if e["type"] == "score"), None)
    if last_stop == STEP and last_score:
        parts = [_reviewer_feedback(last_score["criteria"])]
        if human:
            parts.append(f"[사람 피드백] {human}")
        return "\n".join(parts)
    return human


class CoverLetter:
    name = "cover_letter"
    input_fields = INPUT_FIELDS
    prompt_roles = PROMPT_ROLES

    def clean_input(self, raw: dict) -> dict:
        return clean_fields(INPUT_FIELDS, raw)

    def check_config(self, cfg) -> None:
        for role, text in cfg.prompts.items():
            spec = PROMPT_ROLES.get(role)
            if not spec:
                raise ValueError(f"unknown prompt role {role!r} (roles: {', '.join(PROMPT_ROLES)})")
            check_template(role, text, spec["vars"], spec["required"])

    def default_config(self) -> dict:
        return {
            "workflow": self.name,
            "rubric": DEFAULT_RUBRIC,
            "pass_score": 8.0,
            "max_rounds": 3,
            "budget_usd": 0.5,
        }

    def run_pass(self, ctx: PassContext) -> str:
        cfg, llm, inp = ctx.cfg, ctx.llm, ctx.input
        calls = [e for e in ctx.events if e["type"] == "llm_call"]
        writes = [e for e in calls if e["purpose"] == "write"]
        init: _S = {
            "round": max((e["round"] for e in calls), default=0),  # numbering continues across human revisions
            "in_pass": 0,
            "review_idx": sum(1 for e in calls if e["purpose"] == "review"),
            "draft": writes[-1]["response"] if writes else inp.get("draft", ""),
            "feedback": _opening_feedback(ctx),
            "passed": False,
            "stop_reason": "",
        }

        grounded = _grounded(inp)
        allowed = _allowed_texts(inp, [e["comment"] for e in ctx.events if e["type"] == "gate_decision" and e.get("comment")] + ([ctx.feedback] if ctx.feedback else []))

        def write(s: _S) -> dict:
            r = s["round"] + 1
            try:
                text = llm.complete(
                    agent="writer",
                    purpose="write",
                    round=r,
                    prompt=_writer_prompt(inp, s["draft"], s["feedback"], cfg.prompts.get("writer")),
                    meta={"original": inp.get("draft", ""), "feedback": s["feedback"], "round": r, "grounded": grounded,
                          "evidence_head": inp["evidence"][0]["text"] if grounded else ""},
                )
            except BudgetExceeded:  # refused before sending: nothing was spent
                return {"stop_reason": BUDGET_EXCEEDED}
            out = {"round": r, "in_pass": s["in_pass"] + 1, "draft": text}
            if llm.over_budget():
                out["stop_reason"] = BUDGET_EXCEEDED
            return out

        def review(s: _S) -> dict:
            found = find_unsupported(s["draft"], allowed) if grounded else {"numbers": [], "terms": []}  # code, not a model
            invented = bool(found["numbers"] or found["terms"])
            try:
                raw = llm.complete(
                    agent="reviewer",
                    purpose="review",
                    round=s["round"],
                    prompt=_reviewer_prompt(inp, s["draft"], cfg.rubric, cfg.prompts.get("reviewer"), f"[코드 검사] {describe(found)}\n" if invented else ""),
                    meta={"rubric": [c.name for c in cfg.rubric], "review_index": s["review_idx"], "round": s["round"]},
                )
            except BudgetExceeded:
                return {"stop_reason": BUDGET_EXCEEDED, "passed": False}
            criteria = _parse_review(raw, cfg.rubric)
            total = round(sum(c["score"] * c["weight"] for c in criteria) / sum(c["weight"] for c in criteria), 3)
            over = _over(inp, s["draft"])
            # a high score cannot carry a draft that invents figures or exceeds the limit: those are facts the code measured
            blocked = (["facts"] if invented else []) + (["length"] if over else [])
            passed = total >= cfg.pass_score and not blocked
            ctx.emit("score", round=s["round"], total=total, passed=passed, pass_score=cfg.pass_score, criteria=criteria, blocked_by=blocked,
                     checks={"facts": found, "chars": _count(inp, s["draft"]), "over": over} if (grounded or _limit(inp)) else None)
            fb = _reviewer_feedback(criteria)
            if over:  # code-measured, so the writer is told even if the reviewer missed it
                fb += f"\n- 분량: {_length_note(inp, s['draft']).strip()} 반드시 줄이세요."
            if invented:
                fb += f"\n- 근거 검사: {describe(found)}"
            return {"feedback": fb, "passed": passed, "review_idx": s["review_idx"] + 1}

        def decide(s: _S) -> dict:
            if s["stop_reason"]:  # review was refused by the budget guard
                return {}
            if s["passed"]:
                reason = PASSED
            elif llm.over_budget():
                reason = BUDGET_EXCEEDED
            elif cfg.step_mode:  # the human decides after every round, so the per-pass round cap does not apply
                reason = STEP
            elif s["in_pass"] >= cfg.max_rounds:
                reason = MAX_ROUNDS
            else:
                reason = ""
            return {"stop_reason": reason}

        g = StateGraph(_S)
        g.add_node("write", write)
        g.add_node("review", review)
        g.add_node("decide", decide)
        g.add_edge(START, "write")
        g.add_conditional_edges("write", lambda s: END if s["stop_reason"] else "review")
        g.add_edge("review", "decide")
        g.add_conditional_edges("decide", lambda s: END if s["stop_reason"] else "write")
        final = g.compile().invoke(init)

        ctx.emit("artifact", name="cover_letter", content=final["draft"], round=final["round"])
        return final["stop_reason"]
