"""Run configuration. Snapshotted into the `run_started` event so every run is reproducible."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Criterion(BaseModel):
    name: str
    weight: float = Field(default=1.0, gt=0)
    description: str = ""


class LLMConfig(BaseModel):
    mode: str = "fake"  # "fake" | "real" (real: M2)
    model: str = "fake-1"
    # fake only: total score returned per review call, in order (last value repeats)
    scenario: list[float] = Field(default_factory=lambda: [5.0, 7.0, 9.0], min_length=1)
    delay_ms: int = Field(default=0, ge=0)  # fake only: simulated latency per call (to watch a run live)


class Pricing(BaseModel):
    """USD per 1k tokens. Estimate only: may differ from the actual bill."""

    in_per_1k: float = 0.003
    out_per_1k: float = 0.015


class RunConfig(BaseModel):
    workflow: str
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pricing: Pricing = Field(default_factory=Pricing)
    rubric: list[Criterion] = Field(min_length=1)
    pass_score: float = Field(default=8.0, ge=0, le=10)
    max_rounds: int = Field(default=3, ge=1)  # per pass (a human "revise" starts a new pass)
    budget_usd: float = Field(default=0.5, gt=0)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Dicts merge recursively; anything else (incl. lists) is replaced."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out
