from __future__ import annotations

import json
import random
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_SLUG_RE = re.compile(r"[^a-z0-9-]+")

# Pool of friendly agent names; the orchestrator picks one per task at creation.
_AGENTS_PATH = Path(__file__).resolve().parent / "agents.json"
_AGENT_NAMES_CACHE: Optional[list[str]] = None


def agent_names() -> list[str]:
    global _AGENT_NAMES_CACHE
    if _AGENT_NAMES_CACHE is None:
        try:
            data = json.loads(_AGENTS_PATH.read_text(encoding="utf-8"))
            raw = data.get("agents") if isinstance(data, dict) else data
            _AGENT_NAMES_CACHE = [str(n).strip() for n in (raw or []) if str(n).strip()]
        except Exception:
            _AGENT_NAMES_CACHE = []
    return _AGENT_NAMES_CACHE


def pick_agent_name() -> Optional[str]:
    names = agent_names()
    return random.choice(names) if names else None

# Board states for the built-in tracker.
STATE_TODO = "Todo"
STATE_IN_PROGRESS = "In Progress"
STATE_COMPLETED = "Completed"
STATE_CANCELLED = "Cancelled"
# Legacy states (pre–3-column board); mapped for display and dispatch.
_LEGACY_COMPLETED = frozenset({"Resolved"})
_LEGACY_IN_PROGRESS = frozenset({"Testing"})
BOARD_STATES = [
    STATE_TODO,
    STATE_IN_PROGRESS,
    STATE_COMPLETED,
    STATE_CANCELLED,
]

# Iteration lifecycle: planning → running → testing → completed.
ITERATION_PLANNING = "planning"
ITERATION_RUNNING = "running"
ITERATION_TESTING = "testing"
ITERATION_COMPLETED = "completed"
ITERATION_OPEN_STATES = [ITERATION_PLANNING, ITERATION_RUNNING, ITERATION_TESTING]
ITERATION_STATES = [
    ITERATION_PLANNING,
    ITERATION_RUNNING,
    ITERATION_TESTING,
    ITERATION_COMPLETED,
]

TASK_KIND_TASK = "task"
TASK_KIND_BUG = "bug"
TASK_KINDS = [TASK_KIND_TASK, TASK_KIND_BUG]


def board_column(state: Optional[str]) -> str:
    """Map a task state to a board column (handles legacy Resolved/Testing rows)."""
    raw = (state or "").strip()
    if raw in _LEGACY_COMPLETED:
        return STATE_COMPLETED
    if raw in _LEGACY_IN_PROGRESS:
        return STATE_IN_PROGRESS
    if raw in BOARD_STATES:
        return raw
    return STATE_TODO

# Sentinel so update_task can distinguish "leave priority alone" from "clear it".
_UNSET = object()


class DbError(Exception):
    pass


def slugify(value: str) -> str:
    value = (value or "").strip().lower().replace(" ", "-")
    value = _SLUG_RE.sub("-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_workspace_path(path: Optional[str]) -> Optional[str]:
    """Resolve an existing directory on the server, or None for the default workspace."""
    if path is None:
        return None
    raw = str(path).strip()
    if not raw:
        return None
    resolved = Path(raw).expanduser().resolve()
    if not resolved.is_dir():
        raise DbError(f"workspace path is not a directory: {resolved}")
    return str(resolved)


class Db:
    """SQLite store for the built-in tracker (projects + tasks + comments)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    needs_git INTEGER NOT NULL DEFAULT 0,
                    workspace_path TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_slug TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    identifier TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT,
                    priority INTEGER,
                    state TEXT NOT NULL DEFAULT 'Todo',
                    agent_name TEXT,
                    role TEXT,
                    subdir TEXT,
                    plan_order INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_slug) REFERENCES projects(slug) ON DELETE CASCADE
                );
                -- Planner output: dependency edges between tasks.
                -- kind: 'depends' (task waits for depends_on) or 'tests' (task verifies depends_on).
                CREATE TABLE IF NOT EXISTS task_deps (
                    task_id TEXT NOT NULL,
                    depends_on_task_id TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'depends',
                    PRIMARY KEY (task_id, depends_on_task_id, kind),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY (depends_on_task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                -- Orchestrator data: one row per dispatch/attempt (run), with token + runtime usage.
                -- No FK on task_id so QA runs can omit a task row.
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    identifier TEXT,
                    project_slug TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT,
                    state TEXT,
                    outcome TEXT NOT NULL DEFAULT 'running',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    runtime_seconds REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_slug);
                CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
                CREATE TABLE IF NOT EXISTS iterations (
                    id TEXT PRIMARY KEY,
                    project_slug TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'planning',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (project_slug) REFERENCES projects(slug) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_iterations_project ON iterations(project_slug);
                """
            )
            self._migrate_schema(conn)
            self._migrate_iterations_data(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
            task_cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            for col, ddl in (
                ("agent_name", "ALTER TABLE tasks ADD COLUMN agent_name TEXT"),
                ("role", "ALTER TABLE tasks ADD COLUMN role TEXT"),
                ("subdir", "ALTER TABLE tasks ADD COLUMN subdir TEXT"),
                ("plan_order", "ALTER TABLE tasks ADD COLUMN plan_order INTEGER"),
                ("iteration_id", "ALTER TABLE tasks ADD COLUMN iteration_id TEXT"),
                ("kind", "ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task'"),
            ):
                if col not in task_cols:
                    conn.execute(ddl)
            project_cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
            if "needs_git" not in project_cols:
                conn.execute("ALTER TABLE projects ADD COLUMN needs_git INTEGER NOT NULL DEFAULT 0")
            if "workspace_path" not in project_cols:
                conn.execute("ALTER TABLE projects ADD COLUMN workspace_path TEXT")
            iter_cols = [r[1] for r in conn.execute("PRAGMA table_info(iterations)").fetchall()]
            if "testing_instructions" not in iter_cols:
                conn.execute("ALTER TABLE iterations ADD COLUMN testing_instructions TEXT")

    def _migrate_iterations_data(self, conn: sqlite3.Connection) -> None:
        """Assign legacy tasks to a default iteration per project."""
        projects = [r["slug"] for r in conn.execute("SELECT slug FROM projects").fetchall()]
        for slug in projects:
            has_iteration = conn.execute(
                "SELECT 1 FROM iterations WHERE project_slug = ? LIMIT 1", (slug,)
            ).fetchone()
            if has_iteration:
                continue
            orphan = conn.execute(
                "SELECT 1 FROM tasks WHERE project_slug = ? AND iteration_id IS NULL LIMIT 1",
                (slug,),
            ).fetchone()
            if not orphan:
                self._insert_iteration(conn, slug, "Iteration 1", ITERATION_PLANNING)
                continue
            states = [
                r["state"]
                for r in conn.execute(
                    "SELECT state FROM tasks WHERE project_slug = ? AND iteration_id IS NULL",
                    (slug,),
                ).fetchall()
            ]
            terminal = {STATE_COMPLETED, STATE_CANCELLED}
            if states and all(s in terminal for s in states):
                iter_state = ITERATION_COMPLETED
            elif any(s not in terminal and s != STATE_TODO for s in states):
                iter_state = ITERATION_RUNNING
            else:
                iter_state = ITERATION_PLANNING
            iter_id = self._insert_iteration(conn, slug, "Iteration 1", iter_state)
            conn.execute(
                "UPDATE tasks SET iteration_id = ?, kind = COALESCE(kind, 'task') "
                "WHERE project_slug = ? AND iteration_id IS NULL",
                (iter_id, slug),
            )

    def _insert_iteration(
        self, conn: sqlite3.Connection, project_slug: str, title: str, state: str
    ) -> str:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM iterations WHERE project_slug = ?",
            (project_slug,),
        ).fetchone()["n"]
        iter_id = uuid.uuid4().hex
        now = _now()
        conn.execute(
            """INSERT INTO iterations (id, project_slug, seq, title, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (iter_id, project_slug, seq, title.strip(), state, now, now),
        )
        return iter_id

    # --- projects ---
    def create_project(
        self,
        title: str,
        description: Optional[str],
        slug: Optional[str],
        needs_git: bool = False,
        workspace_path: Optional[str] = None,
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise DbError("title is required")
        if not (slug or "").strip():
            raise DbError("project slug is required")
        slug = slugify(slug)
        if not slug:
            raise DbError("project slug must contain letters or numbers")
        ws_path = _validate_workspace_path(workspace_path)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                raise DbError(f"project slug already exists: {slug}")
            conn.execute(
                "INSERT INTO projects (slug, title, description, needs_git, workspace_path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, title, (description or "").strip() or None, 1 if needs_git else 0, ws_path, _now()),
            )
        self.create_iteration(slug, "Iteration 1")
        return self.get_project(slug)

    def get_project(self, slug: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
            return dict(row) if row else None

    def update_project(
        self,
        slug: str,
        title: str,
        description: Optional[str],
        needs_git: Optional[bool] = None,
        workspace_path=_UNSET,
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise DbError("title is required")
        ws_path = _UNSET
        if workspace_path is not _UNSET:
            ws_path = _validate_workspace_path(workspace_path)
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                raise DbError(f"unknown project: {slug}")
            if needs_git is None and workspace_path is _UNSET:
                conn.execute(
                    "UPDATE projects SET title = ?, description = ? WHERE slug = ?",
                    (title, (description or "").strip() or None, slug),
                )
            elif needs_git is None:
                conn.execute(
                    "UPDATE projects SET title = ?, description = ?, workspace_path = ? WHERE slug = ?",
                    (title, (description or "").strip() or None, ws_path, slug),
                )
            elif workspace_path is _UNSET:
                conn.execute(
                    "UPDATE projects SET title = ?, description = ?, needs_git = ? WHERE slug = ?",
                    (title, (description or "").strip() or None, 1 if needs_git else 0, slug),
                )
            else:
                conn.execute(
                    "UPDATE projects SET title = ?, description = ?, needs_git = ?, workspace_path = ? "
                    "WHERE slug = ?",
                    (
                        title,
                        (description or "").strip() or None,
                        1 if needs_git else 0,
                        ws_path,
                        slug,
                    ),
                )
        return self.get_project(slug)

    def project_needs_git(self, slug: str) -> bool:
        project = self.get_project(slug)
        return bool(project and project.get("needs_git"))

    def delete_project(self, slug: str) -> None:
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                raise DbError(f"unknown project: {slug}")
            # FK cascade (PRAGMA foreign_keys=ON) removes the project's tasks + comments.
            conn.execute("DELETE FROM projects WHERE slug = ?", (slug,))
            # runs has no FK on task_id, so remove its rows explicitly.
            conn.execute("DELETE FROM runs WHERE project_slug = ?", (slug,))

    def list_projects(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
            projects = []
            for row in rows:
                counts = {s: 0 for s in BOARD_STATES}
                for cnt in conn.execute(
                    "SELECT state, COUNT(*) c FROM tasks WHERE project_slug = ? GROUP BY state",
                    (row["slug"],),
                ).fetchall():
                    col = board_column(cnt["state"])
                    counts[col] = counts.get(col, 0) + cnt["c"]
                projects.append({**dict(row), "counts": counts})
            return projects

    # --- iterations ---
    def create_iteration(self, project_slug: str, title: str) -> dict:
        title = (title or "").strip()
        if not title:
            raise DbError("title is required")
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE slug = ?", (project_slug,)).fetchone():
                raise DbError(f"unknown project: {project_slug}")
            open_row = conn.execute(
                f"""SELECT id, state FROM iterations
                   WHERE project_slug = ? AND state IN ({",".join("?" * len(ITERATION_OPEN_STATES))}) LIMIT 1""",
                (project_slug, *ITERATION_OPEN_STATES),
            ).fetchone()
            if open_row:
                raise DbError(
                    f"close or finish {open_row['state']} iteration before starting a new one"
                )
            iter_id = self._insert_iteration(conn, project_slug, title, ITERATION_PLANNING)
        return self.get_iteration(iter_id)

    def get_iteration(self, iteration_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM iterations WHERE id = ?", (iteration_id,)).fetchone()
            return dict(row) if row else None

    def list_iterations(self, project_slug: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM iterations WHERE project_slug = ? ORDER BY seq",
                (project_slug,),
            ).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                counts = {s: 0 for s in BOARD_STATES}
                for cnt in conn.execute(
                    """SELECT state, COUNT(*) c FROM tasks
                       WHERE iteration_id = ? GROUP BY state""",
                    (row["id"],),
                ).fetchall():
                    col = board_column(cnt["state"])
                    counts[col] = counts.get(col, 0) + cnt["c"]
                item["counts"] = counts
                item["task_count"] = conn.execute(
                    "SELECT COUNT(*) c FROM tasks WHERE iteration_id = ? AND kind = 'task'",
                    (row["id"],),
                ).fetchone()["c"]
                item["bug_count"] = conn.execute(
                    "SELECT COUNT(*) c FROM tasks WHERE iteration_id = ? AND kind = 'bug'",
                    (row["id"],),
                ).fetchone()["c"]
                out.append(item)
            return out

    def iterations_in_states(self, states: list[str]) -> list[dict]:
        if not states:
            return []
        placeholders = ",".join("?" * len(states))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM iterations WHERE state IN ({placeholders}) ORDER BY project_slug, seq",
                tuple(states),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_iteration_state(self, iteration_id: str, state: str) -> None:
        if state not in ITERATION_STATES:
            raise DbError(f"invalid iteration state: {state}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE iterations SET state = ?, updated_at = ? WHERE id = ?",
                (state, _now(), iteration_id),
            )

    def update_iteration(
        self,
        iteration_id: str,
        *,
        title: Optional[str] = None,
        testing_instructions: object = _UNSET,
    ) -> dict:
        iteration = self.get_iteration(iteration_id)
        if not iteration:
            raise DbError("unknown iteration")
        state = iteration.get("state")
        updates: list[str] = []
        values: list = []
        if title is not None:
            title = (title or "").strip()
            if not title:
                raise DbError("title is required")
            updates.append("title = ?")
            values.append(title)
        if testing_instructions is not _UNSET:
            if state != ITERATION_PLANNING:
                raise DbError("testing instructions can only be edited while the iteration is in planning")
            text = (testing_instructions or "").strip() if testing_instructions is not None else ""
            updates.append("testing_instructions = ?")
            values.append(text or None)
        if not updates:
            return iteration
        updates.append("updated_at = ?")
        values.append(_now())
        values.append(iteration_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE iterations SET {', '.join(updates)} WHERE id = ?",
                values,
            )
        return self.get_iteration(iteration_id)

    def validate_task_create(self, iteration_id: str, kind: str) -> dict:
        kind = (kind or TASK_KIND_TASK).strip().lower()
        if kind not in TASK_KINDS:
            raise DbError(f"invalid task kind: {kind}")
        iteration = self.get_iteration(iteration_id)
        if not iteration:
            raise DbError("unknown iteration")
        state = iteration.get("state")
        if kind == TASK_KIND_TASK and state != ITERATION_PLANNING:
            raise DbError("tasks can only be added while the iteration is in planning")
        if kind == TASK_KIND_BUG and state != ITERATION_COMPLETED:
            raise DbError("bugs can only be added to a completed iteration")
        return iteration

    def iteration_has_open_work(self, iteration_id: str) -> bool:
        """True if any task or bug in the iteration is not terminal."""
        terminal = {STATE_COMPLETED, STATE_CANCELLED}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state FROM tasks WHERE iteration_id = ?", (iteration_id,)
            ).fetchall()
        return any(r["state"] not in terminal for r in rows)

    def iteration_tasks_all_terminal(self, iteration_id: str) -> bool:
        """True when every task (not bug) in the iteration is Completed/Cancelled."""
        return self._iteration_kind_all_terminal(iteration_id, TASK_KIND_TASK)

    def iteration_build_tasks(self, iteration_id: str) -> list[dict]:
        return [
            t
            for t in self.tasks_for_iteration(iteration_id)
            if (t.get("kind") or TASK_KIND_TASK) == TASK_KIND_TASK
        ]

    def iteration_has_open_build_work(self, iteration_id: str) -> bool:
        """True when any build task is Todo or In Progress."""
        active = {STATE_TODO, STATE_IN_PROGRESS}
        return any(t.get("state") in active for t in self.iteration_build_tasks(iteration_id))

    def get_task_by_identifier(self, project_slug: str, identifier: str) -> Optional[dict]:
        ident = (identifier or "").strip()
        if not ident:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE project_slug = ? AND identifier = ?",
                (project_slug, ident),
            ).fetchone()
            return dict(row) if row else None

    def _iteration_kind_all_terminal(self, iteration_id: str, kind: str) -> bool:
        terminal = {STATE_COMPLETED, STATE_CANCELLED}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state FROM tasks WHERE iteration_id = ? AND kind = ?",
                (iteration_id, kind),
            ).fetchall()
        if not rows:
            return False
        return all(r["state"] in terminal for r in rows)

    def iteration_all_terminal(self, iteration_id: str) -> bool:
        """True when every task and bug in the iteration is terminal."""
        terminal = {STATE_COMPLETED, STATE_CANCELLED}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state FROM tasks WHERE iteration_id = ?", (iteration_id,)
            ).fetchall()
        if not rows:
            return False
        return all(r["state"] in terminal for r in rows)

    def maybe_complete_iteration(self, iteration_id: str) -> bool:
        """Mark iteration completed when all tasks (and any bugs) are done."""
        iteration = self.get_iteration(iteration_id)
        if not iteration or iteration.get("state") == ITERATION_COMPLETED:
            return False
        if not self.iteration_tasks_all_terminal(iteration_id):
            return False
        if not self.iteration_all_terminal(iteration_id):
            return False
        self.set_iteration_state(iteration_id, ITERATION_COMPLETED)
        return True

    # --- tasks ---
    def identifier_taken(self, identifier: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM tasks WHERE identifier = ?", (identifier,)).fetchone() is not None

    def create_task(
        self,
        project_slug: str,
        title: str,
        description: Optional[str],
        priority: Optional[int],
        identifier: Optional[str] = None,
        iteration_id: Optional[str] = None,
        kind: str = TASK_KIND_TASK,
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise DbError("title is required")
        if not iteration_id:
            raise DbError("iteration_id is required")
        self.validate_task_create(iteration_id, kind)
        kind = (kind or TASK_KIND_TASK).strip().lower()
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE slug = ?", (project_slug,)).fetchone():
                raise DbError(f"unknown project: {project_slug}")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM tasks WHERE project_slug = ?",
                (project_slug,),
            ).fetchone()["n"]
            task_id = uuid.uuid4().hex
            if identifier:
                ident = slugify(identifier.replace("_", "-"))
                if not ident:
                    raise DbError("invalid task identifier")
            else:
                prefix = "bug" if kind == TASK_KIND_BUG else slugify(project_slug)
                ident = f"{prefix}-{seq}"
            if conn.execute("SELECT 1 FROM tasks WHERE identifier = ?", (ident,)).fetchone():
                raise DbError(f"task identifier already exists: {ident}")
            now = _now()
            conn.execute(
                """INSERT INTO tasks
                   (id, project_slug, seq, identifier, title, description, priority, state,
                    agent_name, iteration_id, kind, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    project_slug,
                    seq,
                    ident,
                    title,
                    (description or "").strip() or None,
                    priority if isinstance(priority, int) else None,
                    STATE_TODO,
                    pick_agent_name(),
                    iteration_id,
                    kind,
                    now,
                    now,
                ),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def tasks_for_project(self, project_slug: str, iteration_id: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if iteration_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE project_slug = ? AND iteration_id = ? ORDER BY seq",
                    (project_slug, iteration_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE project_slug = ? ORDER BY seq",
                    (project_slug,),
                ).fetchall()
            return [dict(r) for r in rows]

    def tasks_for_iteration(self, iteration_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE iteration_id = ? ORDER BY seq",
                (iteration_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def tasks_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders})", tuple(ids)
            ).fetchall()
            return [dict(r) for r in rows]

    def update_task_state(self, task_id: str, state: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (state, _now(), task_id),
            )

    def update_task(
        self,
        task_id: str,
        state=None,
        priority=_UNSET,
        title=None,
        description=_UNSET,
    ) -> Optional[dict]:
        """Update task fields (state/priority from drag-and-drop; title/description from editor)."""
        existing = self.get_task(task_id)
        if not existing:
            raise DbError(f"unknown task: {task_id}")
        sets: list[str] = []
        params: list = []
        if state is not None:
            if state not in BOARD_STATES:
                raise DbError(f"invalid state: {state}")
            old_col = board_column(existing.get("state"))
            new_col = board_column(state)
            if old_col in {STATE_COMPLETED, STATE_CANCELLED} and new_col in {
                STATE_TODO,
                STATE_IN_PROGRESS,
            }:
                raise DbError("completed tasks cannot be moved back to Todo or In Progress")
            sets.append("state = ?")
            params.append(state)
        if priority is not _UNSET:
            sets.append("priority = ?")
            params.append(priority if isinstance(priority, int) else None)
        if title is not None:
            title = (title or "").strip()
            if not title:
                raise DbError("title is required")
            sets.append("title = ?")
            params.append(title)
        if description is not _UNSET:
            sets.append("description = ?")
            params.append((description or "").strip() or None)
        if not sets:
            return self.get_task(task_id)
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(task_id)
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                raise DbError(f"unknown task: {task_id}")
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))
        return self.get_task(task_id)

    def downstream_iteration(self, project_slug: str, source_iteration_id: str) -> Optional[dict]:
        """Next planning iteration after source (by seq), if any."""
        source = self.get_iteration(source_iteration_id)
        if not source or source.get("project_slug") != project_slug:
            return None
        source_seq = int(source.get("seq") or 0)
        candidates = [
            i
            for i in self.list_iterations(project_slug)
            if int(i.get("seq") or 0) > source_seq and i.get("state") == ITERATION_PLANNING
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda i: int(i.get("seq") or 0))

    def get_or_create_downstream_iteration(self, project_slug: str, source_iteration_id: str) -> tuple[dict, bool]:
        """Return the planning iteration downstream of source, creating one when allowed."""
        source = self.get_iteration(source_iteration_id)
        if not source or source.get("project_slug") != project_slug:
            raise DbError("unknown iteration")
        existing = self.downstream_iteration(project_slug, source_iteration_id)
        if existing:
            return existing, False
        if source.get("state") != ITERATION_COMPLETED:
            raise DbError("finish the current iteration before copying tasks downstream")
        if any(i.get("state") in ITERATION_OPEN_STATES for i in self.list_iterations(project_slug)):
            raise DbError("close or finish the open iteration before copying tasks downstream")
        source_seq = int(source.get("seq") or 0)
        created = self.create_iteration(project_slug, f"Iteration {source_seq + 1}")
        return created, True

    def copy_task_downstream(self, project_slug: str, task_id: str) -> dict:
        """Copy a task into the downstream planning iteration (Todo)."""
        task = self.get_task(task_id)
        if not task or task.get("project_slug") != project_slug:
            raise DbError("unknown task")
        source_iteration_id = task.get("iteration_id")
        if not source_iteration_id:
            raise DbError("task has no iteration")
        target, created = self.get_or_create_downstream_iteration(project_slug, source_iteration_id)
        kind = (task.get("kind") or TASK_KIND_TASK).strip().lower()
        if kind == TASK_KIND_BUG:
            raise DbError("bugs cannot be copied downstream — add bugs directly on the completed iteration")
        copied = self.create_task(
            project_slug,
            task.get("title") or "",
            task.get("description"),
            task.get("priority"),
            iteration_id=target["id"],
            kind=TASK_KIND_TASK,
        )
        return {"task": copied, "iteration": target, "created_iteration": created, "source_task_id": task_id}

    def delete_task(self, task_id: str) -> None:
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
                raise DbError(f"unknown task: {task_id}")
            conn.execute("DELETE FROM runs WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def board(self, project_slug: str, iteration_id: Optional[str] = None) -> dict:
        grouped: dict[str, list[dict]] = {s: [] for s in BOARD_STATES}
        for task in self.tasks_for_project(project_slug, iteration_id=iteration_id):
            task["deps"] = self.deps_for_task(task["id"])
            col = board_column(task.get("state"))
            grouped[col].append(task)
        return grouped

    # --- planner / dependencies ---
    def set_task_plan(
        self,
        task_id: str,
        role: Optional[str] = None,
        subdir: Optional[str] = None,
        plan_order: Optional[int] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET role = ?, subdir = ?, plan_order = ?, updated_at = ? WHERE id = ?",
                (role, subdir, plan_order, _now(), task_id),
            )

    def clear_task_deps_for_iteration(self, iteration_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM task_deps WHERE task_id IN (SELECT id FROM tasks WHERE iteration_id = ?)",
                (iteration_id,),
            )

    def clear_task_deps_for_project(self, project_slug: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM task_deps WHERE task_id IN (SELECT id FROM tasks WHERE project_slug = ?)",
                (project_slug,),
            )

    def add_task_dep(self, task_id: str, depends_on_task_id: str, kind: str = "depends") -> None:
        if task_id == depends_on_task_id:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_deps (task_id, depends_on_task_id, kind) VALUES (?, ?, ?)",
                (task_id, depends_on_task_id, kind),
            )

    def remove_task_dep(self, task_id: str, depends_on_task_id: str, kind: str = "depends") -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM task_deps WHERE task_id = ? AND depends_on_task_id = ? AND kind = ?",
                (task_id, depends_on_task_id, kind),
            )

    def deps_for_task(self, task_id: str, kind: str = "depends") -> list[dict]:
        """Return the tasks this task depends on (or tests), with their current state."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.id, t.identifier, t.state, d.kind
                   FROM task_deps d JOIN tasks t ON t.id = d.depends_on_task_id
                   WHERE d.task_id = ? AND d.kind = ?""",
                (task_id, kind),
            ).fetchall()
            return [dict(r) for r in rows]

    def tests_targets(self, task_id: str) -> list[dict]:
        """Return the tasks that this (test) task verifies."""
        return self.deps_for_task(task_id, kind="tests")

    def test_tasks_for_build(self, build_task_id: str) -> list[dict]:
        """Return test tasks that verify the given build task."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.*
                   FROM task_deps d JOIN tasks t ON t.id = d.task_id
                   WHERE d.depends_on_task_id = ? AND d.kind = 'tests'""",
                (build_task_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- comments ---
    def add_comment(self, task_id: str, body: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO comments (id, task_id, body, created_at) VALUES (?, ?, ?, ?)",
                (uuid.uuid4().hex, task_id, body, _now()),
            )

    # --- runs (orchestrator data: tokens + runtime per dispatch/attempt) ---
    def create_run(
        self,
        task_id: Optional[str],
        identifier: Optional[str],
        project_slug: Optional[str],
        attempt: int,
        session_id: Optional[str] = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO runs
                   (id, task_id, identifier, project_slug, attempt, session_id, outcome, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'running', ?)""",
                (run_id, task_id, identifier, project_slug, int(attempt or 0), session_id, _now()),
            )
        return run_id

    def finalize_run(
        self,
        run_id: str,
        *,
        outcome: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        turn_count: int = 0,
        runtime_seconds: float = 0.0,
        session_id: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE runs SET
                       outcome = ?, input_tokens = ?, output_tokens = ?, total_tokens = ?,
                       turn_count = ?, runtime_seconds = ?, session_id = ?, state = ?,
                       error = ?, finished_at = ?
                   WHERE id = ?""",
                (
                    outcome,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    int(total_tokens or 0),
                    int(turn_count or 0),
                    float(runtime_seconds or 0.0),
                    session_id,
                    state,
                    error,
                    _now(),
                    run_id,
                ),
            )

    def finalize_dangling_runs(self, outcome: str = "interrupted") -> int:
        """Close out runs left in 'running' by a process that died/was killed
        before it could finalize them (otherwise they show forever as Running)."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE runs SET outcome = ?, finished_at = ? "
                "WHERE outcome = 'running' AND finished_at IS NULL",
                (outcome, _now()),
            )
            return cur.rowcount

    def list_runs(self, project_slug: Optional[str] = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 500))
        sql = """
            SELECT r.*,
                   t.kind AS task_kind,
                   i.title AS iteration_title,
                   i.seq AS iteration_seq
            FROM runs r
            LEFT JOIN tasks t ON t.id = r.task_id
            LEFT JOIN iterations i ON i.id = t.iteration_id
        """
        with self._connect() as conn:
            if project_slug:
                rows = conn.execute(
                    sql + " WHERE r.project_slug = ? ORDER BY r.started_at DESC LIMIT ?",
                    (project_slug, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    sql + " ORDER BY r.started_at DESC LIMIT ?", (limit,)
                ).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                kind = (item.pop("task_kind", None) or "task").strip().lower()
                item["kind"] = kind if kind in TASK_KINDS else TASK_KIND_TASK
                title = (item.pop("iteration_title", None) or "").strip()
                seq = item.pop("iteration_seq", None)
                item["iteration"] = title or (f"Iteration {seq}" if seq else None)
                out.append(item)
            return out

    def project_run_totals(self, project_slug: str) -> dict:
        """Sum tokens and runtime from finalized runs for one project."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(runtime_seconds), 0) AS seconds_running
                   FROM runs
                   WHERE project_slug = ? AND outcome != 'running'""",
                (project_slug,),
            ).fetchone()
        return {
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "total_tokens": int(row["total_tokens"]),
            "seconds_running": float(row["seconds_running"]),
        }
