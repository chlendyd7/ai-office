from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    model: str | None = None  # the model that actually answered (falls back to client.model)
    attempts: int = 1  # HTTP attempts it took (>1 means retries happened)
    served_model: str | None = None  # model id the gateway reports having used (may differ from the requested alias)
    stop_reason: str | None = None  # "end_turn", "max_tokens" (the answer was cut off), ...
    # web search (only when tools were given): what the model actually retrieved, as opposed to what it says it read
    searches: int = 0  # searches the gateway ran for this call
    queries: list[str] | None = None
    sources: list[dict] | None = None  # [{"url", "title", "page_age"}] from the search results themselves
    segments: list[dict] | None = None  # the answer in pieces: [{"text", "cites": [url, ...]}] (cites = the sources the gateway attached to that piece)


class LLMClient(Protocol):
    model: str

    def complete(self, *, agent: str, purpose: str, prompt: str, meta: dict, attachments: list[dict] | None = None,
                 tools: list[dict] | None = None, tool_choice: dict | None = None) -> LLMResult:
        """`meta` is for FakeLLM only (scenario hooks). Real clients must ignore it.
        `attachments`: [{"kind": "pdf" | "image", "media_type": ..., "data": <base64>}] sent along with the prompt.
        `tools` / `tool_choice`: server-side tools such as web search, passed through as the gateway expects them."""
        ...
