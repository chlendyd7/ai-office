from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int


class LLMClient(Protocol):
    model: str

    def complete(self, *, agent: str, purpose: str, prompt: str, meta: dict) -> LLMResult:
        """`meta` is for FakeLLM only (scenario hooks). Real clients must ignore it."""
        ...
