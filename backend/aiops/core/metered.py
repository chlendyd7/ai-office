"""The only LLM handle workflows get. Every call is recorded and costed here, so it can't be bypassed."""
from __future__ import annotations

import time

from ..llm.base import LLMClient
from .config import RunConfig
from .store import RunStore


class MeteredLLM:
    def __init__(self, inner: LLMClient, store: RunStore, run_id: str, cfg: RunConfig, spent_usd: float = 0.0):
        self._inner = inner
        self._store = store
        self._run_id = run_id
        self._cfg = cfg
        self.spent_usd = spent_usd  # cumulative across passes (incl. before a human revise)

    def over_budget(self) -> bool:
        return self.spent_usd >= self._cfg.budget_usd

    def complete(self, *, agent: str, purpose: str, round: int, prompt: str, meta: dict | None = None) -> str:
        self._store.append(self._run_id, "call_started", agent=agent, purpose=purpose, round=round)
        t0 = time.perf_counter()
        res = self._inner.complete(agent=agent, purpose=purpose, prompt=prompt, meta=meta or {})
        latency_ms = int((time.perf_counter() - t0) * 1000)
        p = self._cfg.pricing
        cost = round_(res.tokens_in / 1000 * p.in_per_1k + res.tokens_out / 1000 * p.out_per_1k)
        self.spent_usd += cost
        self._store.append(
            self._run_id,
            "llm_call",
            agent=agent,
            purpose=purpose,
            round=round,
            model=self._inner.model,
            prompt=prompt,
            response=res.text,
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
        return res.text


def round_(x: float) -> float:
    return float(f"{x:.6f}")
