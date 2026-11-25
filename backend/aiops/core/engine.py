"""Run lifecycle: start -> loop pass -> awaiting_approval -> (approve | reject | revise -> another pass).

AI may run loops; only a human decision finalizes a run.
"""
from __future__ import annotations

from typing import Callable

from ..llm.factory import make_llm
from ..workflows.base import BUDGET_EXCEEDED, Workflow, PassContext
from .config import RunConfig, deep_merge
from .metered import MeteredLLM
from .store import RunStore
from .summary import derive_status

DECISIONS = ("approve", "revise", "reject")


class GateError(Exception):
    """Decision not allowed in the run's current state."""


class Engine:
    def __init__(self, store: RunStore, workflows: dict[str, Workflow], llm_factory: Callable = make_llm):
        self.store = store
        self.workflows = workflows
        self.llm_factory = llm_factory

    def start(self, workflow: str, input: dict, overrides: dict | None = None) -> str:
        run_id = self.create(workflow, input, overrides)
        self.run_pass(run_id, feedback=None)
        return run_id

    def create(self, workflow: str, input: dict, overrides: dict | None = None, parent: str | None = None) -> str:
        """Record the run (config snapshot + input) without running it. Lets callers run the first pass in background."""
        wf = self.workflows[workflow]
        cfg = RunConfig.model_validate(deep_merge(wf.default_config(), overrides or {}))
        self.llm_factory(cfg.llm)  # fail fast on a bad llm config, before anything is recorded
        run_id = self.store.new_run_id()
        self.store.append(run_id, "run_started", run_id=run_id, workflow=workflow, config=cfg.model_dump(), input=input,
                          parent=parent)  # parent: the run this one re-executes (None for a fresh run)
        return run_id

    def decide(self, run_id: str, decision: str, comment: str = "", actor: str = "human") -> None:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        status = derive_status(self.store.read(run_id))
        if status != "awaiting_approval":
            raise GateError(f"run {run_id} is {status}, not awaiting_approval")
        if decision == "revise" and not comment.strip():
            raise ValueError("revise requires a comment")
        self.store.append(run_id, "gate_decision", decision=decision, comment=comment, actor=actor)
        if decision == "approve":
            self.store.append(run_id, "run_finished", status="approved", reason="approved")
        elif decision == "reject":
            self.store.append(run_id, "run_finished", status="rejected", reason=comment or "rejected")
        else:
            self.run_pass(run_id, feedback=comment)

    def run_pass(self, run_id: str, feedback: str | None = None) -> None:
        events = self.store.read(run_id)
        started = events[0]
        cfg = RunConfig.model_validate(started["config"])
        spent = sum(e["cost_usd"] for e in events if e["type"] == "llm_call")
        llm = MeteredLLM(self.llm_factory(cfg.llm), self.store, run_id, cfg, spent_usd=spent)
        ctx = PassContext(
            cfg=cfg,
            input=started["input"],
            events=events,
            feedback=feedback,
            llm=llm,
            emit=lambda t, **d: self.store.append(run_id, t, **d),
        )
        try:
            reason = BUDGET_EXCEEDED if llm.over_budget() else self.workflows[started["workflow"]].run_pass(ctx)
        except Exception as e:  # recorded, never swallowed: status becomes "failed"
            self.store.append(run_id, "error", message=f"{type(e).__name__}: {e}")
            self.store.append(run_id, "run_finished", status="failed", reason="error")
            return
        self.store.append(run_id, "loop_stopped", reason=reason)
        self.store.append(run_id, "gate_opened", reason=reason)
