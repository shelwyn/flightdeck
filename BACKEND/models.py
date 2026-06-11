from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Blocker:
    id: Optional[str] = None
    identifier: Optional[str] = None
    state: Optional[str] = None

    def to_dict(self) -> dict:
        return {"id": self.id, "identifier": self.identifier, "state": self.state}


@dataclass
class Issue:
    id: str
    identifier: str
    title: str
    state: str
    description: Optional[str] = None
    priority: Optional[int] = None
    branch_name: Optional[str] = None
    url: Optional[str] = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[Blocker] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    agent_name: Optional[str] = None
    role: Optional[str] = None
    subdir: Optional[str] = None
    plan_order: Optional[int] = None
    iteration_id: Optional[str] = None
    kind: Optional[str] = None

    def state_lower(self) -> str:
        return (self.state or "").strip().lower()

    def to_template_dict(self) -> dict:
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "state": self.state,
            "description": self.description or "",
            "priority": self.priority,
            "branch_name": self.branch_name,
            "url": self.url,
            "labels": list(self.labels),
            "blocked_by": [b.to_dict() for b in self.blocked_by],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "agent_name": self.agent_name,
            "role": self.role,
            "subdir": self.subdir,
            "plan_order": self.plan_order,
            "iteration_id": self.iteration_id,
            "kind": self.kind,
        }
