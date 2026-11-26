"""Offices: one per domain (career now; code and planning later). The database stays domain-free: everything an office
knows about its own work -- the fields of a project's brief, the kinds of deliverables and their specs, the stages of its
pipeline -- is declared here, in code, and stored as JSON. See docs/design-task-schema.md.

(Not to be confused with `aiops/office/`, the decision-maker screen's view code.)"""
from __future__ import annotations

from dataclasses import dataclass, field

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

    def check_role(self, role: str) -> None:
        if role not in self.resource_roles:
            raise ValueError(f"unknown resource role {role!r} for office {self.key!r} (roles: {', '.join(self.resource_roles)})")


from .career import CAREER  # noqa: E402  (after Office: career builds one)

OFFICES: dict[str, Office] = {o.key: o for o in (CAREER,)}
