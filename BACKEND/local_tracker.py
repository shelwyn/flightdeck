from __future__ import annotations

from db import Db, board_column
from models import Blocker, Issue
from tracker import TrackerError


def _to_issue(row: dict) -> Issue:
    return Issue(
        id=row["id"],
        identifier=row["identifier"],
        title=row["title"],
        state=board_column(row.get("state")),
        description=row.get("description"),
        priority=row.get("priority"),
        branch_name=None,
        url=None,
        labels=[],
        blocked_by=[],
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        agent_name=row.get("agent_name"),
        role=row.get("role"),
        subdir=row.get("subdir"),
        plan_order=row.get("plan_order"),
        iteration_id=row.get("iteration_id"),
        kind=row.get("kind") or "task",
    )


class LocalTracker:
    """Tracker backend over the built-in SQLite store, scoped to one project.

    Implements the same surface the orchestrator uses for LinearClient.
    """

    def __init__(self, db: Db, project_slug: str, iteration_id: str | None = None):
        self.db = db
        self.project_slug = project_slug
        self.iteration_id = iteration_id

    def _project_tasks(self) -> list[dict]:
        return self.db.tasks_for_project(self.project_slug, iteration_id=self.iteration_id)

    def _issue_with_deps(self, row: dict) -> Issue:
        """Build an Issue and populate blocked_by from the planner's task_deps so
        the orchestrator's existing blocker gate sequences tasks correctly."""
        issue = _to_issue(row)
        try:
            deps = self.db.deps_for_task(row["id"], kind="depends")
        except Exception:
            deps = []
        issue.blocked_by = [
            Blocker(id=d["id"], identifier=d.get("identifier"), state=board_column(d.get("state")))
            for d in deps
        ]
        return issue

    def fetch_candidate_issues(self, active_states: list[str]) -> list[Issue]:
        active = {s.strip().lower() for s in active_states}
        try:
            tasks = self._project_tasks()
        except Exception as exc:
            raise TrackerError("local_query", str(exc))
        return [
            self._issue_with_deps(t)
            for t in tasks
            if board_column(t.get("state")).strip().lower() in active
        ]

    def fetch_issues_by_states(self, states: list[str]) -> list[Issue]:
        if not states:
            return []
        wanted = {s.strip().lower() for s in states}
        try:
            tasks = self._project_tasks()
        except Exception as exc:
            raise TrackerError("local_query", str(exc))
        return [
            self._issue_with_deps(t)
            for t in tasks
            if board_column(t.get("state")).strip().lower() in wanted
        ]

    def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if not issue_ids:
            return []
        try:
            tasks = self.db.tasks_by_ids(issue_ids)
        except Exception as exc:
            raise TrackerError("local_query", str(exc))
        if self.iteration_id:
            tasks = [t for t in tasks if t.get("iteration_id") == self.iteration_id]
        return [self._issue_with_deps(t) for t in tasks]

    def add_comment(self, issue_id: str, body: str) -> bool:
        try:
            self.db.add_comment(issue_id, body)
        except Exception as exc:
            raise TrackerError("local_write", str(exc))
        return True

    def set_issue_state(self, issue_id: str, state_name: str) -> bool:
        try:
            self.db.update_task_state(issue_id, state_name)
        except Exception as exc:
            raise TrackerError("local_write", str(exc))
        return True
