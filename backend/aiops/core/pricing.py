"""Verified per-model prices (pricing.json, USD per 1M tokens) -> RunConfig.pricing_models (per 1k)."""
from __future__ import annotations

import json
from pathlib import Path


def read_pricing_file(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def load_prices(path: str | Path) -> dict[str, dict]:
    return {
        model: {"in_per_1k": v["in_per_1m"] / 1000, "out_per_1k": v["out_per_1m"] / 1000}
        for model, v in read_pricing_file(path).get("models", {}).items()
    }
