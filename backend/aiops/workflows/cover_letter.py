"""Cover-letter (자기소개서) writer <-> reviewer loop. First workload on the AI Ops core."""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..core.jsonx import extract_json
from ..core.metered import BudgetExceeded
from .base import BUDGET_EXCEEDED, MAX_ROUNDS, PASSED, STEP, PassContext

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


def _length_note(inp: dict, draft: str) -> str:
    """Measured by code: an LLM reviewer cannot count characters, and a limit is a fact, not an opinion."""
    lim = _limit(inp)
    if not lim:
        return ""
    n = len(draft)
    return f"[실제 글자 수] {n}자 (제한 {lim}자, {f'초과 {n - lim}자' if n > lim else '준수'})\n"


def _writer_prompt(inp: dict, draft: str, feedback: str) -> str:
    limit = f"[분량 제한] {_limit(inp)}자 이내\n" if _limit(inp) else ""
    return (
        "당신은 자기소개서 작성 전문가입니다. 아래 문항에 맞게 자기소개서를 개선하세요.\n"
        f"[지원 직무] {inp.get('job', '')}\n[문항] {inp.get('question', '')}\n{limit}"
        f"[현재 초안]\n{draft}\n"
        f"[피드백]\n{feedback or '없음 (첫 작성)'}\n"
        "[규칙] 지원자가 제공한 초안과 사람 피드백에 없는 경험·수치·기술·성과를 지어내지 마세요. "
        "구체화가 필요하지만 근거가 없으면 [수치 입력 필요]처럼 빈칸으로 표시하세요.\n"
        "개선된 자기소개서 본문만 출력하세요."
    )


def _reviewer_prompt(inp: dict, draft: str, rubric) -> str:
    rb = "\n".join(f"- {c.name} (가중치 {c.weight}): {c.description}" for c in rubric)
    return (
        "당신은 엄격한 자기소개서 심사위원입니다. 루브릭 각 항목을 0~10점으로 채점하고, "
        "항목별로 개선 방향을 한 문장으로 적어 JSON으로만 출력하세요.\n"
        f"[지원 직무] {inp.get('job', '')}\n[문항] {inp.get('question', '')}\n"
        f"[루브릭]\n{rb}\n[자기소개서]\n{draft}\n{_length_note(inp, draft)}"
        '출력 형식: {"criteria":[{"name":"항목명","score":0-10,"feedback":"..."}]}'
    )


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

        def write(s: _S) -> dict:
            r = s["round"] + 1
            try:
                text = llm.complete(
                    agent="writer",
                    purpose="write",
                    round=r,
                    prompt=_writer_prompt(inp, s["draft"], s["feedback"]),
                    meta={"original": inp.get("draft", ""), "feedback": s["feedback"], "round": r},
                )
            except BudgetExceeded:  # refused before sending: nothing was spent
                return {"stop_reason": BUDGET_EXCEEDED}
            out = {"round": r, "in_pass": s["in_pass"] + 1, "draft": text}
            if llm.over_budget():
                out["stop_reason"] = BUDGET_EXCEEDED
            return out

        def review(s: _S) -> dict:
            try:
                raw = llm.complete(
                    agent="reviewer",
                    purpose="review",
                    round=s["round"],
                    prompt=_reviewer_prompt(inp, s["draft"], cfg.rubric),
                    meta={"rubric": [c.name for c in cfg.rubric], "review_index": s["review_idx"], "round": s["round"]},
                )
            except BudgetExceeded:
                return {"stop_reason": BUDGET_EXCEEDED, "passed": False}
            criteria = _parse_review(raw, cfg.rubric)
            total = round(sum(c["score"] * c["weight"] for c in criteria) / sum(c["weight"] for c in criteria), 3)
            passed = total >= cfg.pass_score
            ctx.emit("score", round=s["round"], total=total, passed=passed, pass_score=cfg.pass_score, criteria=criteria)
            fb = _reviewer_feedback(criteria)
            if _limit(inp) and len(s["draft"]) > _limit(inp):  # code-measured, so the writer is told even if the reviewer missed it
                fb += f"\n- 분량: {_length_note(inp, s['draft']).strip()} 반드시 줄이세요."
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
