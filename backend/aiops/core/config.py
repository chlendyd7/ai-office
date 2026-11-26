"""Run configuration. Snapshotted into the `run_started` event so every run is reproducible.
Never contains secrets: API keys/endpoints come from environment variables only."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class Criterion(BaseModel):
    name: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0)
    description: str = ""


class LLMConfig(BaseModel):
    mode: str = "fake"  # "fake" | "hchat"
    model: str = "fake-1"  # default model; hchat requires an explicit one
    models: dict[str, str] = Field(default_factory=dict)  # per-agent override, e.g. {"reviewer": "claude-haiku-4-5"}
    max_tokens: int = Field(default=2000, ge=16)  # per call output cap; also bounds the worst-case cost of a call
    timeout_s: float = Field(default=60, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)  # 429 / 5xx / network errors only
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
    pricing_models: dict[str, Pricing] = Field(default_factory=dict)  # per-model override of `pricing`
    rubric: list[Criterion] = Field(min_length=1)
    pass_score: float = Field(default=8.0, ge=0, le=10)
    max_rounds: int = Field(default=3, ge=1)  # per pass (a human "revise" starts a new pass)
    budget_usd: float = Field(default=0.5, gt=0)
    step_mode: bool = False  # pause for a human decision after every round, not only when the loop ends
    prompts: dict[str, str] = Field(default_factory=dict)  # role -> template text; a role left out uses the workflow's built-in prompt
    prompt_refs: dict[str, str] = Field(default_factory=dict)  # role -> "name@version" it came from (provenance only)

    @field_validator("rubric")
    @classmethod
    def _unique_names(cls, v: list[Criterion]) -> list[Criterion]:
        names = [c.name.strip() for c in v]
        if len(set(names)) != len(names):  # a duplicate would silently overwrite its twin when scores are looked up by name
            raise ValueError("rubric criterion names must be unique")
        return v

    def price_for(self, model: str) -> Pricing:
        return self.pricing_models.get(model, self.pricing)

    def worst_prices(self) -> Pricing:
        """Highest configured prices: used to bound a call's cost before the model is known."""
        ps = [self.pricing, *self.pricing_models.values()]
        return Pricing(in_per_1k=max(p.in_per_1k for p in ps), out_per_1k=max(p.out_per_1k for p in ps))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Dicts merge recursively; anything else (incl. lists) is replaced."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out
