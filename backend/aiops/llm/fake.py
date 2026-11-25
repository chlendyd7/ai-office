"""Deterministic LLM for verifying loop logic, token accounting and UI without an API key."""
from __future__ import annotations

import json
import time

from ..core.config import LLMConfig
from .base import LLMResult


def _est_tokens(s: str) -> int:
    # Rough estimate (~2 chars/token for Korean). Real clients report actual usage.
    return max(1, len(s) // 2)


class FakeLLM:
    def __init__(self, cfg: LLMConfig):
        self.model = cfg.model
        self.scenario = cfg.scenario
        self.delay = cfg.delay_ms / 1000

    def complete(self, *, agent: str, purpose: str, prompt: str, meta: dict) -> LLMResult:
        if self.delay:
            time.sleep(self.delay)
        if purpose == "write":
            text = self._write(meta)
        elif purpose == "review":
            text = self._review(meta)
        else:
            raise ValueError(f"FakeLLM: unknown purpose {purpose!r}")
        return LLMResult(text=text, tokens_in=_est_tokens(prompt), tokens_out=_est_tokens(text))

    def _write(self, meta: dict) -> str:
        fb = (meta.get("feedback") or "").strip().replace("\n", " ")[:80]
        return f"{meta.get('original', '')}\n[R{meta['round']} 개선본] 반영한 피드백: {fb or '없음'}"

    def _review(self, meta: dict) -> str:
        i = meta["review_index"]
        target = self.scenario[min(i, len(self.scenario) - 1)]
        criteria = [
            {"name": n, "score": target, "feedback": f"{n}: 목표 {target}점 수준으로 보완 필요 (가짜 리뷰어)"}
            for n in meta["rubric"]
        ]
        return json.dumps({"criteria": criteria}, ensure_ascii=False)


def make_llm(cfg: LLMConfig):
    if cfg.mode == "fake":
        return FakeLLM(cfg)
    raise NotImplementedError(f"llm.mode={cfg.mode!r} is not implemented yet (real LLM: M2)")
