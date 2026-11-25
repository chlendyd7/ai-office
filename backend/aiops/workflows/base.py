"""Workflow contract. A workflow runs one "pass" (a loop until it stops) and reports why it stopped.

Metering, recording, budget state and the human approval gate belong to the core, not the workflow.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..core.config import RunConfig
from ..core.metered import MeteredLLM

PASSED = "passed"
MAX_ROUNDS = "max_rounds"
BUDGET_EXCEEDED = "budget_exceeded"


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
