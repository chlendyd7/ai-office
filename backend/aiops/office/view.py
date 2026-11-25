"""What the Office surface may see: status, final result, final score, decisions, usage totals (tokens, cost,
budget) and who is doing what. Never prompts or responses: that detail lives in AI Ops (the popup)."""
from __future__ import annotations


def office_view(s: dict) -> dict:
    running = s["status"] == "running"  # mid-run scores/drafts are process data, not a result to judge
    last = s["scores"][-1] if s["scores"] and not running else None
    return {
        "run_id": s["run_id"],
        "workflow": s["workflow"],
        "status": s["status"],
        "stop_reason": s["stop_reason"],
        "error": bool(s["error"]),  # detail stays in Ops
        "job": s["input"].get("job", ""),
        "question": s["input"].get("question", ""),
        "result": s["result"]["content"] if s["result"] and not running else None,
        "score": {"total": last["total"], "pass_score": s["pass_score"], "passed": last["passed"]} if last else None,
        "rounds": s["rounds"],
        "decisions": s["decisions"],
        "usage": {
            "tokens_in": s["totals"]["tokens_in"],
            "tokens_out": s["totals"]["tokens_out"],
            "cost_usd": s["totals"]["cost_usd"],
            "budget_usd": s["budget_usd"],
            "calls": s["totals"]["calls"],
            "failed_calls": s["failed_calls"],
        },
    }


def activity(ev: dict) -> dict | None:
    """Run event -> animation-only event for the office floor (None = not relevant)."""
    t = ev["type"]
    if t == "run_started":
        return {"k": "begin"}
    if t == "call_started":
        return {"k": "start", "agent": ev["agent"], "purpose": ev["purpose"], "round": ev["round"]}
    if t in ("llm_call", "call_failed"):
        return {"k": "end", "agent": ev["agent"]}
    if t == "gate_opened":
        return {"k": "gate"}
    if t == "gate_decision":
        return {"k": "decision", "decision": ev["decision"]}
    if t == "run_finished":
        return {"k": "finished", "status": ev["status"]}
    return None
