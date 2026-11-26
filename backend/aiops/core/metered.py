"""The only LLM handle workflows get. Every call is recorded and costed here, so it can't be bypassed."""
from __future__ import annotations

import time

from ..llm.base import LLMClient
from .config import RunConfig
from .store import RunStore


class BudgetExceeded(Exception):
    """A call was refused because it could push the run past its budget (nothing was sent)."""


class MeteredLLM:
    def __init__(self, inner: LLMClient, store: RunStore, run_id: str, cfg: RunConfig, spent_usd: float = 0.0):
        self._inner = inner
        self._store = store
        self._run_id = run_id
        self._cfg = cfg
        self.spent_usd = spent_usd  # cumulative across passes (incl. before a human revise)
        self.last_stop_reason: str | None = None  # of the latest call: "max_tokens" means the answer was cut off

    def over_budget(self) -> bool:
        return self.spent_usd >= self._cfg.budget_usd

    def _guard(self, prompt: str, extra_in_tokens: int = 0) -> None:
        """Refuse a call whose worst case (estimated input + max_tokens output) would exceed the budget.
        `extra_in_tokens`: what attachments (PDF pages, images) may add, which the prompt text does not show."""
        p = self._cfg.worst_prices()
        worst = (len(prompt) // 2 + extra_in_tokens) / 1000 * p.in_per_1k + self._cfg.llm.max_tokens / 1000 * p.out_per_1k
        if self.spent_usd + worst > self._cfg.budget_usd:
            raise BudgetExceeded(
                f"worst-case cost ${worst:.4f} + spent ${self.spent_usd:.4f} exceeds budget ${self._cfg.budget_usd}"
            )

    def complete(self, *, agent: str, purpose: str, round: int, prompt: str, meta: dict | None = None,
                 attachments: list[dict] | None = None) -> str:
        """`attachments`: [{"kind", "media_type", "data" (base64), "est_tokens", ...}]. The record keeps only their sizes
        and estimates, never the file content."""
        self._guard(prompt, sum(a.get("est_tokens", 0) for a in attachments or []))
        attached = {"attachments": [{k: v for k, v in a.items() if k != "data"} | {"bytes": len(a["data"]) * 3 // 4} for a in attachments]} if attachments else {}
        self._store.append(self._run_id, "call_started", agent=agent, purpose=purpose, round=round)
        t0 = time.perf_counter()
        try:
            extra = {"attachments": attachments} if attachments else {}  # only clients that take attachments are given the key
            res = self._inner.complete(agent=agent, purpose=purpose, prompt=prompt, meta=meta or {}, **extra)
        except Exception as e:
            self._store.append(
                self._run_id, "call_failed", agent=agent, purpose=purpose, round=round, prompt=prompt,
                error=f"{type(e).__name__}: {e}", latency_ms=int((time.perf_counter() - t0) * 1000), **attached,
            )
            raise
        self.last_stop_reason = res.stop_reason
        latency_ms = int((time.perf_counter() - t0) * 1000)
        model = res.model or self._inner.model
        p = self._cfg.price_for(model)
        cost = round_(res.tokens_in / 1000 * p.in_per_1k + res.tokens_out / 1000 * p.out_per_1k)
        self.spent_usd += cost
        self._store.append(
            self._run_id,
            "llm_call",
            agent=agent,
            purpose=purpose,
            round=round,
            model=model,
            prompt=prompt,
            response=res.text,
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            cost_usd=cost,
            latency_ms=latency_ms,
            attempts=res.attempts,
            served_model=res.served_model,
            **attached,
            **({"stop_reason": res.stop_reason} if res.stop_reason else {}),
        )
        return res.text


def round_(x: float) -> float:
    return float(f"{x:.6f}")
