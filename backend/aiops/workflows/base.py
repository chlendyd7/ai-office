"""Workflow contract. A workflow runs one "pass" (a loop until it stops) and reports why it stopped.

Metering, recording, budget state and the human approval gate belong to the core, not the workflow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Protocol

from ..core.config import RunConfig
from ..core.metered import MeteredLLM

PASSED = "passed"
MAX_ROUNDS = "max_rounds"
BUDGET_EXCEEDED = "budget_exceeded"
STEP = "step"  # one round done; paused for a human because cfg.step_mode is on


@dataclass
class PassContext:
    cfg: RunConfig
    input: dict
    events: list[dict]  # run record up to the start of this pass
    feedback: str | None  # human revise comment (None on the first pass)
    llm: MeteredLLM
    emit: Callable[..., dict]  # emit(type, **data) -> appends to the run record


class Workflow(Protocol):
    name: str

    def default_config(self) -> dict: ...

    def run_pass(self, ctx: PassContext) -> str:
        """Run the loop until it stops. Emit `artifact` for the result. Return a stop reason."""
        ...


# ---- prompt templates and declared inputs (shared by workflows) ----
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def template_vars(template: str) -> set[str]:
    return set(_VAR.findall(template))


def render(template: str, values: dict[str, str]) -> str:
    """Replaces {{name}}. Plain replacement (no format()), so braces in the text, e.g. JSON examples, are safe."""
    return _VAR.sub(lambda m: str(values[m.group(1)]), template)


def check_template(role: str, template: str, allowed: list[str], required: list[str]) -> None:
    used = template_vars(template)
    unknown = sorted(used - set(allowed))
    if unknown:
        raise ValueError(f"prompt '{role}': unknown variable {{{{{unknown[0]}}}}} (allowed: {', '.join(allowed)})")
    missing = [v for v in required if v not in used]
    if missing:
        raise ValueError(f"prompt '{role}': must contain {{{{{missing[0]}}}}}")


def clean_fields(fields: list[dict], raw: dict) -> dict:
    """Validates a workflow input against its declared fields: required present, numbers cast, unknown keys dropped."""
    out = {}
    for f in fields:
        v = raw.get(f["name"])
        v = v.strip() if isinstance(v, str) else v
        if v in (None, ""):
            if f.get("required"):
                raise ValueError(f"'{f['name']}' is required")
            continue
        if f.get("type") == "number":
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                raise ValueError(f"'{f['name']}' must be a number, got {v!r}")
            if v <= 0:
                raise ValueError(f"'{f['name']}' must be positive")
        out[f["name"]] = v
    return out
