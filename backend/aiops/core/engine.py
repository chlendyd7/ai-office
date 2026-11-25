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
    def __init__(self, store: RunStore, workflows: dict[str, Workflow], llm_factory: Callable = make_llm,
                 prices: dict[str, dict] | None = None):
        self.store = store
        self.workflows = workflows
        self.llm_factory = llm_factory
        self.prices = prices or {}  # verified per-model prices; become part of each run's config snapshot

    def validate(self, workflow: str, overrides: dict | None = None) -> RunConfig:
        """Resolve the final config and fail fast (bad rubric, unknown mode, missing env) before anything is recorded."""
        wf = self.workflows[workflow]  # KeyError: unknown workflow
        base = deep_merge({"pricing_models": self.prices}, wf.default_config())
        cfg = RunConfig.model_validate(deep_merge(base, overrides or {}))
        self.llm_factory(cfg.llm)
        return cfg

    def recover_interrupted(self) -> list[str]:
        """Call once at process start: a run still marked "running" has no live thread, so the process that ran it died.
        Close it as failed instead of leaving a zombie that blocks the queue and the Office."""
        fixed = []
        for rid in self.store.list_runs():
            if derive_status(self.store.read(rid)) == "running":
                self.store.append(rid, "error", message="InterruptedError: the server stopped while this run was in progress")
                self.store.append(rid, "run_finished", status="failed", reason="interrupted")
                fixed.append(rid)
        return fixed

    def start(self, workflow: str, input: dict, overrides: dict | None = None) -> str:
        run_id = self.create(workflow, input, overrides)
        self.run_pass(run_id, feedback=None)
        return run_id

    def create(self, workflow: str, input: dict, overrides: dict | None = None, parent: str | None = None) -> str:
        """Record the run (config snapshot + input) without running it. Lets callers run the first pass in background."""
        cfg = self.validate(workflow, overrides)
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
