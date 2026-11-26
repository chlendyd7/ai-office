"""Offices: one per domain (career now; code and planning later). The database stays domain-free: everything an office
knows about its own work -- the fields of a project's brief, the kinds of deliverables and their specs, the stages of its
pipeline -- is declared here, in code, and stored as JSON. See docs/design-task-schema.md.

(Not to be confused with `aiops/office/`, the decision-maker screen's view code.)"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..workflows.base import clean_fields


@dataclass(frozen=True)
class Office:
    key: str  # the value of projects.office
    label: str
    brief_fields: list[dict]  # what a project of this office asks for (same field format as a workflow's input_fields)
    resource_roles: list[str]  # what a resource can be for a project: evidence | brief | reference ...
    deliverable_kinds: dict[str, dict]  # kind -> {"label", "workflow", "spec_fields"}
    stages: list[str] = field(default_factory=list)  # pipeline stage names, in order
    title_fields: tuple[str, ...] = ()  # brief fields that make up a project's title
    checker: Callable[[dict, list[dict], list[dict]], dict] | None = None  # (brief, deliverables, resources) -> {"blocking", "warnings"}
    role_labels: dict[str, str] = field(default_factory=dict)  # for screens: role key -> what to call it
    default_library_role: str = ""  # the role a resource picked from my library gets unless told otherwise
    default_own_role: str = ""  # the role of text or a file given for this project alone
    agents: list[dict] = field(default_factory=list)  # who works here: {"key", "label", "stage", "prompt_role"}; their models are in the settings
    run_settings_fields: list[dict] = field(default_factory=list)  # a project's settings (models per agent, loop limits, budget...)
    run_overrides: Callable[[dict], dict] | None = None  # cleaned settings -> overrides for the workflow's RunConfig
    build_input: Callable[[dict, dict, dict], dict] | None = None  # (project, deliverable, snapshot) -> the workflow input of one run

    def clean_brief(self, raw: dict) -> dict:
        return clean_fields(self.brief_fields, raw)

    def title_for(self, brief: dict) -> str:
        return " · ".join(str(brief[k]) for k in self.title_fields if brief.get(k)) or self.label

    def kind(self, kind: str) -> dict:
        if kind not in self.deliverable_kinds:
            raise ValueError(f"unknown deliverable kind {kind!r} for office {self.key!r} (kinds: {', '.join(self.deliverable_kinds)})")
        return self.deliverable_kinds[kind]

    def clean_spec(self, kind: str, raw: dict) -> dict:
        return clean_fields(self.kind(kind)["spec_fields"], raw)

    def measure(self, kind: str, spec: dict, content: str) -> dict:
        """What the code can measure about a finished deliverable (cover letter: its length by the question's counting rule)."""
        fn = self.kind(kind).get("measure")
        return fn(spec, content) if fn else {}

    def check_project(self, brief: dict, deliverables: list[dict], resources: list[dict]) -> dict:
        """Is the project ready to run? `blocking` must be fixed first; `warnings` may be ignored."""
        return self.checker(brief, deliverables, resources) if self.checker else {"blocking": [], "warnings": []}

    def declaration(self) -> dict:
        """What a screen needs to build its forms (functions are left out)."""
        return {"key": self.key, "label": self.label, "brief_fields": self.brief_fields, "resource_roles": self.resource_roles, "stages": self.stages,
                "role_labels": {r: self.role_labels.get(r, r) for r in self.resource_roles},
                "default_library_role": self.default_library_role or self.resource_roles[0],
                "default_own_role": self.default_own_role or self.resource_roles[0],
                "agents": self.agents, "run_settings_fields": self.run_settings_fields,
                "deliverable_kinds": {k: {"label": v["label"], "workflow": v["workflow"], "spec_fields": v["spec_fields"]} for k, v in self.deliverable_kinds.items()}}

    def clean_settings(self, raw: dict) -> dict:
        """Settings with every default filled in, so a screen always shows what will actually be used."""
        return clean_fields(self.run_settings_fields, raw)

    def overrides_for(self, settings: dict) -> dict:
        return self.run_overrides(self.clean_settings(settings)) if self.run_overrides else {}

    def input_for(self, project: dict, deliverable: dict, snapshot: dict) -> dict:
        """What one run of a deliverable is given (cover letter: the question, the company, the job description and the passages
        of the user's verified material that matter for it)."""
        if not self.build_input:
            raise ValueError(f"office {self.key!r} cannot run deliverables yet")
        return self.build_input(project, deliverable, snapshot)

    def check_role(self, role: str) -> None:
        if role not in self.resource_roles:
            raise ValueError(f"unknown resource role {role!r} for office {self.key!r} (roles: {', '.join(self.resource_roles)})")


from .career import CAREER  # noqa: E402  (after Office: career builds one)

OFFICES: dict[str, Office] = {o.key: o for o in (CAREER,)}
