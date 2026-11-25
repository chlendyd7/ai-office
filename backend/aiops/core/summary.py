"""Derives everything shown in Ops/Office from the event log. Nothing here is stored separately."""
from __future__ import annotations


def _bucket() -> dict:
    return {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}


def _add(b: dict, e: dict) -> None:
    b["calls"] += 1
    b["tokens_in"] += e["tokens_in"]
    b["tokens_out"] += e["tokens_out"]
    b["cost_usd"] = round(b["cost_usd"] + e["cost_usd"], 6)


def derive_status(events: list[dict]) -> str:
    status = "running"
    for e in events:
        t = e["type"]
        if t == "gate_opened":
            status = "awaiting_approval"
        elif t == "gate_decision" and e["decision"] == "revise":
            status = "running"
        elif t == "run_finished":
            status = e["status"]
    return status


def summarize(events: list[dict]) -> dict:
    started = events[0]
    cfg = started["config"]
    total, by_agent, by_round, by_model = _bucket(), {}, {}, {}
    scores, decisions = [], []
    stop_reason = result = error = None
    failed_calls = 0
    for e in events:
        t = e["type"]
        if t == "call_failed":
            failed_calls += 1
        elif t == "llm_call":
            _add(total, e)
            _add(by_agent.setdefault(e["agent"], _bucket()), e)
            _add(by_round.setdefault(e["round"], _bucket()), e)
            _add(by_model.setdefault(e["model"], _bucket()), e)
        elif t == "score":
            scores.append({"round": e["round"], "total": e["total"], "passed": e["passed"], "criteria": e["criteria"]})
        elif t == "loop_stopped":
            stop_reason = e["reason"]
        elif t == "artifact":
            result = {"name": e["name"], "content": e["content"], "round": e["round"]}
        elif t == "gate_decision":
            decisions.append({k: e[k] for k in ("ts", "decision", "comment", "actor")})
        elif t == "error":
            error = e["message"]
    return {
        "run_id": started.get("run_id"),
        "parent": started.get("parent"),
        "workflow": started["workflow"],
        "status": derive_status(events),
        "stop_reason": stop_reason,
        "error": error,
        "input": started["input"],
        "result": result,
        "totals": total,
        "failed_calls": failed_calls,
        "by_agent": by_agent,
        "by_round": by_round,
        "by_model": by_model,
        "scores": scores,
        "decisions": decisions,
        "rounds": max(by_round, default=0),
        "budget_usd": cfg["budget_usd"],
        "pass_score": cfg["pass_score"],
        "max_rounds": cfg["max_rounds"],
    }
