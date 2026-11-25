from __future__ import annotations

from ..core.config import LLMConfig
from .fake import FakeLLM


def make_llm(cfg: LLMConfig):
    if cfg.mode == "fake":
        return FakeLLM(cfg)
    if cfg.mode == "hchat":
        from .hchat import HChatClient  # imported lazily: fake mode must not need httpx/env

        return HChatClient(cfg)
    raise ValueError(f"unsupported llm.mode {cfg.mode!r} (expected 'fake' or 'hchat')")
