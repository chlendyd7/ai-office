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

    def complete(self, *, agent: str, purpose: str, prompt: str, meta: dict, attachments: list[dict] | None = None,
                 tools: list[dict] | None = None, tool_choice: dict | None = None) -> LLMResult:
        if self.delay:
            time.sleep(self.delay)
        if purpose == "research":  # clearly fake: example.com addresses, one cited piece and one uncited piece
            company = meta.get("company", "회사")
            text = f"{company}의 사업 개요 (가짜 조사).\n추가로 확인되지 않은 내용도 있다."
            src = [{"url": "https://example.com/fake-source-1", "title": f"{company} 소개 (가짜)", "page_age": ""},
                   {"url": "https://example.com/fake-source-2", "title": f"{company} 소식 (가짜)", "page_age": ""}]
            return LLMResult(text=text, tokens_in=_est_tokens(prompt), tokens_out=_est_tokens(text), searches=1, queries=[f"{company} 소개"], sources=src,
                             segments=[{"text": f"{company}의 사업 개요 (가짜 조사).", "cites": [src[0]["url"]]}, {"text": "추가로 확인되지 않은 내용도 있다.", "cites": []}])
        if purpose == "write":
            text = self._write(meta)
        elif purpose == "review":
            text = self._review(meta)
        elif purpose == "convert":  # one marker per page, like the real instruction asks for
            text = "\n".join(f"=== 쪽 {i} ===\n[가짜 변환 {i}쪽]" for i in range(1, meta.get("pages", 1) + 1))
        else:
            raise ValueError(f"FakeLLM: unknown purpose {purpose!r}")
        return LLMResult(text=text, tokens_in=_est_tokens(prompt), tokens_out=_est_tokens(text))

    def _write(self, meta: dict) -> str:
        if meta.get("grounded"):  # a project run: say only what the material says (no invented figures), like an honest writer
            return f"{meta.get('original') or meta.get('evidence_head', '')}\n[개선본]"
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
