from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import planner
from agent_runner import AgentWorker
from config import ConfigManager
from db import (
    Db,
    DbError,
    STATE_COMPLETED,
    STATE_IN_PROGRESS,
    STATE_TODO,
)
from local_tracker import LocalTracker
from model_settings import ModelSettings
from pi_models import list_pi_models, pi_subprocess_env
from logging_setup import get_logger, kv
from models import Issue
from tracker import TrackerError

log = get_logger("orch.core")

TESTING_AGENT_ID = "__flightdeck_testing__"
TESTING_AGENT_IDENTIFIER = "qa-agent"

_CONTINUATION_DELAY_MS = 1000
_FAILURE_BASE_MS = 10000


@dataclass
class RunningEntry:
    worker: AgentWorker
    issue: Issue
    identifier: str
    started_monotonic: float
    retry_attempt: int
    started_at_iso: str = ""
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    thread_id: Optional[str] = None
    codex_app_server_pid: Optional[int] = None
    last_event: Optional[str] = None
    last_message: Optional[str] = None
    last_timestamp: Optional[str] = None
    last_activity_monotonic: float = 0.0
    turn_count: int = 0
    last_reported_input: int = 0
    last_reported_output: int = 0
    last_reported_total: int = 0
    workspace_path: Optional[Path] = None
    workspace_snapshot: dict[str, float] = field(default_factory=dict)
    is_testing: bool = False


@dataclass
class RetryEntry:
    issue_id: str
    identifier: Optional[str]
    attempt: int
    due_at_monotonic: float
    timer: threading.Timer
    error: Optional[str] = None
    due_at_epoch: float = 0.0


@dataclass
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: float = 0.0


@dataclass
class State:
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    totals: Totals = field(default_factory=Totals)


class Orchestrator:
    def __init__(self, config_manager: ConfigManager, port: Optional[int] = None):
        self.cm = config_manager
        self.port = port
        self.state = State()
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._running_flag = threading.Event()
        # DATA/ lives alongside BACKEND/FRONTEND/WORKSPACES (one level up from this file).
        default_db = Path(__file__).resolve().parent.parent / "DATA" / "tracker.db"
        self.db = Db(Path(os.environ.get("FLIGHTDECK_DB", default_db)))
        self.model_settings = ModelSettings(
            Path(__file__).resolve().parent.parent / "DATA" / "model_config.json"
        )
        self.tracker = None
        self.orchestrating = False
        self.active_project_slug: Optional[str] = None
        self.active_iteration_id: Optional[str] = None
        self.planning = False
        self.planning_project_slug: Optional[str] = None
        self.planning_iteration_id: Optional[str] = None
        # Tasks completed since the last Git commit (drives per-sprint commits).
        self._work_since_commit = 0
        self._completed_since_commit: list[dict] = []
        self._skip_testing_for_run = False
        self.testing_status = "idle"  # idle | standby | running | passed
        self.testing_last_event: Optional[str] = None
        self.testing_last_message: Optional[str] = None
        self._orchestrate_generation = 0

    # --- lifecycle ---
    def run(self) -> int:
        config = self.cm.current
        errors = config.validate_for_dispatch()
        if errors:
            for err in errors:
                log.error("startup validation failed: %s", err)
            return 1

        self._running_flag.set()
        # Close out any runs orphaned by a prior session that was killed before it
        # could finalize them (these were the perpetual "Running" rows).
        try:
            orphaned = self.db.finalize_dangling_runs("interrupted")
            if orphaned:
                log.info("marked %d orphaned run(s) as interrupted", orphaned)
        except Exception as exc:
            log.warning("could not clean up orphaned runs: %s", exc)
        self._maybe_start_server()

        self.reconcile_stuck_iterations()
        log.info("orchestrator started (idle) %s", kv(workflow=str(self.cm.path)))

        threading.Thread(target=self._ticker, daemon=True).start()

        while self._running_flag.is_set():
            try:
                event = self._events.get()
            except (EOFError, KeyboardInterrupt):
                break
            if event[0] == "stop":
                break
            try:
                self._dispatch_event(event)
            except Exception:
                log.exception("error handling event %s", event[0])
        self._shutdown()
        return 0

    def stop(self) -> None:
        self._running_flag.clear()
        self._events.put(("stop",))

    def is_active(self) -> bool:
        """True while planning, orchestrating, or an agent worker is running."""
        return bool(self.orchestrating or self.planning or self.state.running)

    def _shutdown(self) -> None:
        log.info("orchestrator shutting down")
        for entry in list(self.state.running.values()):
            self._add_runtime_seconds(entry)
            entry.worker.terminate()
            self._finalize_run(entry, "stopped")
        self.state.running.clear()
        for retry in list(self.state.retry_attempts.values()):
            retry.timer.cancel()

    def _maybe_start_server(self) -> None:
        port = self.port if self.port is not None else self.cm.current.server_port()
        if port is None:
            return
        try:
            from web import start_server

            start_server(self, host=self.cm.current.server_host(), port=port)
        except Exception:
            log.exception("failed to start HTTP dashboard server")

    def _ticker(self) -> None:
        while self._running_flag.is_set():
            interval = self.cm.current.poll_interval_ms() / 1000.0
            time.sleep(max(1.0, interval))
            if self._running_flag.is_set():
                self._events.put(("tick",))

    def _dispatch_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "tick":
            self._on_tick()
        elif kind == "retry":
            self._on_retry(event[1])
        elif kind == "codex_update":
            self._on_codex_update(event[1], event[2])
        elif kind == "worker_exit":
            verdict = event[4] if len(event) > 4 else None
            self._on_worker_exit(event[1], event[2], event[3], verdict)
        elif kind == "orchestrate":
            generation = event[3] if len(event) > 3 else None
            self._on_orchestrate(event[1], event[2], generation)
        elif kind == "pause":
            done = event[1] if len(event) > 1 else None
            outcome = event[2] if len(event) > 2 else None
            try:
                result = self._on_pause()
                if outcome is not None and isinstance(result, dict):
                    outcome.update(result)
            finally:
                if done is not None:
                    done.set()

    # --- orchestration control (local tracker, driven by the dashboard) ---
    def orchestrate(self, project_slug: str, iteration_id: str, *, generation: Optional[int] = None) -> None:
        self._require_model_setup()
        self._validate_orchestrate_target(project_slug, iteration_id)
        gen = self._orchestrate_generation if generation is None else generation
        self._events.put(("orchestrate", project_slug, iteration_id, gen))

    def plan_and_orchestrate(self, project_slug: str, iteration_id: str) -> None:
        self._require_model_setup()
        self._validate_orchestrate_target(project_slug, iteration_id)
        """Run the LLM planner (off the event loop) to sequence the iteration's
        Todo tasks, persist the plan, then begin orchestration."""
        self.planning = True
        self.planning_project_slug = project_slug
        self.planning_iteration_id = iteration_id
        generation = self._orchestrate_generation

        def _work():
            try:
                self.plan_project(project_slug, iteration_id)
            except Exception:
                log.exception("planning failed for %s", project_slug)
            finally:
                self.planning = False
                self.planning_project_slug = None
                self.planning_iteration_id = None
            if generation != self._orchestrate_generation:
                log.info("plan/orchestrate aborted after stop %s", kv(project=project_slug))
                return
            self.orchestrate(project_slug, iteration_id, generation=generation)

        threading.Thread(target=_work, daemon=True).start()

    def plan_project(self, project_slug: str, iteration_id: str) -> None:
        if not self.db.get_project(project_slug):
            log.error("plan failed: unknown project %s", project_slug)
            return
        tracker = LocalTracker(self.db, project_slug, iteration_id)
        todo = tracker.fetch_candidate_issues([STATE_TODO])
        if not todo:
            log.info("planner: no Todo tasks to plan for %s", project_slug)
            return
        from workspace import WorkspaceManager

        cwd = self._ensure_project_dir(project_slug)
        plan = planner.plan_tasks(
            self.cm.current,
            project_slug,
            cwd,
            todo,
            pi_command=self.pi_command(),
            model_ref=self.pi_model_ref(),
        )
        ident_to_id = {i.identifier: i.id for i in todo}
        self.db.clear_task_deps_for_iteration(iteration_id)
        for order, entry in enumerate(plan):
            task_id = ident_to_id.get(entry.get("identifier"))
            if not task_id:
                continue
            self.db.set_task_plan(
                task_id, subdir=entry.get("subdir"), plan_order=order
            )
            for dep_ident in entry.get("depends_on") or []:
                dep_id = ident_to_id.get(dep_ident)
                if dep_id:
                    self.db.add_task_dep(task_id, dep_id, "depends")
        self._repair_plan_deps(project_slug, iteration_id)
        log.info("plan persisted %s", kv(project=project_slug, iteration=iteration_id, tasks=len(plan)))

    def pause_orchestration(self, timeout: float = 15.0) -> dict:
        """Stop orchestration and wait until iteration state is reconciled."""
        self._orchestrate_generation += 1
        done = threading.Event()
        outcome: dict = {"orchestrating": False}
        self._events.put(("pause", done, outcome))
        if not done.wait(timeout):
            outcome["error"] = "pause timed out"
            self.orchestrating = False
            self.planning = False
        self.reconcile_stuck_iterations()
        return outcome

    def _reconcile_iteration_after_pause(self, iteration_id: str, skip_testing: bool) -> Optional[dict]:
        from db import ITERATION_COMPLETED, ITERATION_PLANNING, ITERATION_RUNNING, ITERATION_TESTING

        iteration = self.db.get_iteration(iteration_id)
        if not iteration:
            return None
        state = iteration.get("state")
        if skip_testing:
            if self.db.iteration_all_terminal(iteration_id):
                self.db.maybe_complete_iteration(iteration_id)
            elif state in (ITERATION_RUNNING, ITERATION_TESTING):
                self.db.set_iteration_state(iteration_id, ITERATION_COMPLETED)
        elif state in (ITERATION_RUNNING, ITERATION_TESTING):
            self.db.set_iteration_state(iteration_id, ITERATION_PLANNING)
        return self.db.get_iteration(iteration_id)

    def reconcile_stuck_iterations(self) -> None:
        """While idle, iterations must not stay running/testing in the database."""
        if self.orchestrating or self.planning:
            return
        from db import ITERATION_PLANNING, ITERATION_RUNNING, ITERATION_TESTING

        for iteration in self.db.iterations_in_states([ITERATION_RUNNING, ITERATION_TESTING]):
            iteration_id = iteration["id"]
            if self.db.iteration_tasks_all_terminal(iteration_id):
                self.db.maybe_complete_iteration(iteration_id)
            else:
                self.db.set_iteration_state(iteration_id, ITERATION_PLANNING)
            log.info("reconciled stuck iteration %s", kv(iteration=iteration_id))

    def remove_project_workspace(self, project_slug: str) -> None:
        """Best-effort removal of the default WORKSPACES/<slug>/ folder on project delete.

        Never deletes an external workspace_path the user chose at project creation."""
        if self._project_workspace_path(project_slug):
            return
        import shutil

        from workspace import WorkspaceManager

        try:
            path = WorkspaceManager(lambda: self.cm.current).project_dir(project_slug)
            root = self.cm.current.workspace_root()
            if path != root and root in path.parents:
                shutil.rmtree(path, ignore_errors=True)
        except Exception as exc:
            log.warning("could not remove workspace for %s: %s", project_slug, exc)

    def create_iteration(self, project_slug: str, title: str) -> dict:
        if not self.db.get_project(project_slug):
            raise ValueError("project not found")
        iteration = self.db.create_iteration(project_slug, title)
        log.info("iteration created %s", kv(project=project_slug, iteration=iteration.get("id")))
        return iteration

    def create_task(
        self,
        project_slug: str,
        iteration_id: str,
        title: str,
        description: Optional[str] = None,
        priority: Optional[int] = None,
        kind: str = "task",
    ) -> dict:
        """Create a task or bug with an LLM-generated readable identifier (via Pi)."""
        self._require_model_setup()
        if self.orchestrating and self.active_project_slug == project_slug:
            raise ValueError("cannot add items while orchestration is running")
        iteration = self.db.get_iteration(iteration_id)
        if not iteration or iteration.get("project_slug") != project_slug:
            raise ValueError("iteration not found")
        self.db.validate_task_create(iteration_id, kind)
        import naming

        cwd = self._ensure_project_dir(project_slug)
        existing = [t["identifier"] for t in self.db.tasks_for_project(project_slug)]
        ident = naming.suggest_task_identifier(
            self.cm.current,
            cwd,
            project_slug,
            title,
            description,
            existing,
            pi_command=self.pi_command(),
            model_ref=self.pi_model_ref(),
        )
        return self.db.create_task(
            project_slug,
            title,
            description,
            priority,
            identifier=ident,
            iteration_id=iteration_id,
            kind=kind,
        )

    def _validate_orchestrate_target(self, project_slug: str, iteration_id: str) -> None:
        from db import ITERATION_COMPLETED, ITERATION_PLANNING, ITERATION_RUNNING, ITERATION_TESTING, TASK_KIND_BUG

        iteration = self.db.get_iteration(iteration_id)
        if not iteration or iteration.get("project_slug") != project_slug:
            raise ValueError("iteration not found")
        state = iteration.get("state")
        if state in (ITERATION_RUNNING, ITERATION_TESTING):
            raise ValueError("iteration is already running")
        if state == ITERATION_PLANNING:
            tasks = self.db.tasks_for_iteration(iteration_id)
            if not any((t.get("kind") or "task") == "task" for t in tasks):
                raise ValueError("add at least one task before orchestrating")
            instructions = (iteration.get("testing_instructions") or "").strip()
            if not instructions:
                raise ValueError(
                    "Add testing instructions for the QA agent before starting orchestration"
                )
            return
        if state == ITERATION_COMPLETED:
            bugs = [
                t
                for t in self.db.tasks_for_iteration(iteration_id)
                if (t.get("kind") or "task") == TASK_KIND_BUG and t.get("state") == STATE_TODO
            ]
            if not bugs:
                raise ValueError("add bugs to this completed iteration before orchestrating")
            return
        raise ValueError(f"cannot orchestrate iteration in state {state}")

    def update_iteration(
        self, project_slug: str, iteration_id: str, *, testing_instructions: Optional[str] = None
    ) -> dict:
        iteration = self.db.get_iteration(iteration_id)
        if not iteration or iteration.get("project_slug") != project_slug:
            raise ValueError("iteration not found")
        try:
            return self.db.update_iteration(iteration_id, testing_instructions=testing_instructions)
        except DbError as exc:
            raise ValueError(str(exc)) from exc

    def copy_task_downstream(self, project_slug: str, task_id: str) -> dict:
        try:
            return self.db.copy_task_downstream(project_slug, task_id)
        except DbError as exc:
            raise ValueError(str(exc)) from exc

    def delete_task(self, project_slug: str, task_id: str) -> None:
        """Remove a task from the tracker; stop any in-flight agent work first."""
        task = self.db.get_task(task_id)
        if not task or task.get("project_slug") != project_slug:
            raise ValueError("task not found")
        if task_id in self.state.running:
            self._terminate_running(task_id, cleanup_workspace=False, release=True)
        self._cancel_retry(task_id)
        self.state.claimed.discard(task_id)
        self.state.completed.discard(task_id)
        try:
            self.db.delete_task(task_id)
        except DbError as exc:
            raise ValueError(str(exc)) from exc
        log.info("task deleted %s", kv(project=project_slug, identifier=task.get("identifier")))
        self._events.put(("tick",))

    def pi_command(self) -> str:
        return self.cm.current.codex_command()

    def pi_model_ref(self, model_override: Optional[str] = None) -> Optional[str]:
        if model_override is not None:
            ref = str(model_override).strip()
            return ref or None
        return self.model_settings.get_model()

    def model_setup_status(self) -> dict:
        return self.model_settings.to_api()

    def list_available_models(self, scope: str = "configured") -> dict:
        from pi_models import list_pi_configured_models, list_pi_models

        if (scope or "").strip().lower() == "all":
            return list_pi_models()
        return list_pi_configured_models()

    def test_model_setup(
        self,
        prompt: str,
        model: Optional[str] = None,
    ) -> dict:
        from llm_helper import query_pi_model

        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        cwd = Path(__file__).resolve().parent.parent / "WORKSPACES" / "_model-test"
        cwd.mkdir(parents=True, exist_ok=True)
        model_ref = (model or "").strip() or None
        return query_pi_model(
            self.cm.current,
            prompt,
            cwd,
            model_override=model_ref,
        )

    def confirm_model_setup(self, model: Optional[str] = None) -> dict:
        model_ref = (model or "").strip() or None
        saved = self.model_settings.save(model_ref)
        log.info("model setup saved %s", kv(model=saved.get("label") or "Pi default"))
        return saved

    def logout_model_setup(self) -> dict:
        saved = self.model_settings.logout()
        log.info("model setup cleared (logout)")
        return saved

    def _require_model_setup(self) -> None:
        if not self.model_settings.is_configured():
            raise ValueError("model setup is required before using the agent")

    def test_model(self, prompt: str) -> dict:
        self._require_model_setup()
        return self.test_model_setup(prompt, model=self.model_settings.get_model())

    def _project_workspace_path(self, slug: str) -> Optional[str]:
        project = self.db.get_project(slug)
        if not project:
            return None
        raw = (project.get("workspace_path") or "").strip()
        return raw or None

    def _project_base(self, project_slug: str) -> Path:
        from workspace import WorkspaceManager

        return WorkspaceManager(lambda: self.cm.current).project_dir(
            project_slug, self._project_workspace_path(project_slug)
        )

    def _ensure_project_dir(self, project_slug: str) -> Path:
        from workspace import WorkspaceManager

        return WorkspaceManager(lambda: self.cm.current).ensure_project_dir(
            project_slug, self._project_workspace_path(project_slug)
        )

    def browse_directories(self, path: Optional[str] = None) -> dict:
        """List subdirectories at a server path (for the project-folder picker)."""
        start = Path(path).expanduser().resolve() if path else Path.home()
        if not start.is_dir():
            raise ValueError(f"not a directory: {start}")
        parent = start.parent
        parent_path = str(parent) if parent != start else None
        directories = []
        try:
            for entry in sorted(start.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and not entry.name.startswith("."):
                    directories.append({"name": entry.name, "path": str(entry.resolve())})
        except OSError as exc:
            raise ValueError(f"cannot read directory: {exc}") from exc
        return {"path": str(start), "parent": parent_path, "directories": directories}

    # --- artifacts: files the agents produced under the project workspace ---
    _ARTIFACT_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pi"}
    _ARTIFACT_MAX_BYTES = 256 * 1024

    def list_project_files(self, project_slug: str) -> list[dict]:
        base = self._project_base(project_slug)
        if not base.exists() or not base.is_dir():
            return []
        files: list[dict] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(base).parts
            if any(part in self._ARTIFACT_SKIP_DIRS for part in rel_parts):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            files.append(
                {
                    "path": path.relative_to(base).as_posix(),
                    "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                }
            )
        return files

    def save_task_upload(self, project_slug: str, identifier: str, filename: str, data: bytes) -> str:
        """Drop an uploaded file into the task's workspace so the agent finds it
        in its working directory when it runs."""
        from workspace import WorkspaceManager, sanitize_key

        base = self._project_base(project_slug)
        task_dir = (base / sanitize_key(identifier)).resolve()
        if task_dir != base and base not in task_dir.parents:
            raise ValueError("task dir escapes project workspace")
        safe = Path(filename or "").name  # strip any directory components
        if not safe or safe in (".", ".."):
            raise ValueError("invalid filename")
        target = (task_dir / safe).resolve()
        if target.parent != task_dir:
            raise ValueError("filename escapes task workspace")
        task_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        log.info("upload saved %s", kv(identifier=identifier, file=safe, bytes=len(data)))
        return target.relative_to(base).as_posix()

    def read_project_file(self, project_slug: str, rel_path: str) -> Optional[dict]:
        base = self._project_base(project_slug)
        target = self._resolve_project_file(base, rel_path)
        if target is None:
            return None
        if not target.is_file():
            return None
        data = target.read_bytes()
        truncated = len(data) > self._ARTIFACT_MAX_BYTES
        chunk = data[: self._ARTIFACT_MAX_BYTES]
        try:
            content = chunk.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            content = None
            binary = True
        return {
            "path": target.relative_to(base).as_posix(),
            "size": len(data),
            "binary": binary,
            "truncated": truncated,
            "content": content,
            "editable": not binary and not truncated,
        }

    def write_project_file(self, project_slug: str, rel_path: str, content: str) -> dict:
        """Save text content to a file under the project workspace (Artifacts editor)."""
        base = self._project_base(project_slug)
        target = self._resolve_project_file(base, rel_path)
        if target is None:
            raise ValueError("invalid file path")
        rel_parts = target.relative_to(base).parts
        if any(part in self._ARTIFACT_SKIP_DIRS for part in rel_parts):
            raise ValueError("cannot write to this path")
        if not target.is_file():
            raise ValueError("file not found")
        data = content.encode("utf-8")
        if len(data) > self._ARTIFACT_MAX_BYTES:
            raise ValueError(f"file exceeds {self._ARTIFACT_MAX_BYTES // 1024} KB limit")
        target.write_bytes(data)
        log.info("artifact saved %s", kv(project=project_slug, path=rel_path, bytes=len(data)))
        return {
            "path": target.relative_to(base).as_posix(),
            "size": len(data),
            "saved": True,
        }

    def create_project_file(self, project_slug: str, rel_path: str, content: str) -> dict:
        """Create a new text file under the project workspace (Artifacts new-file dialog)."""
        rel_path = (rel_path or "").strip().replace("\\", "/")
        if not rel_path:
            raise ValueError("path is required")
        base = self._project_base(project_slug)
        target = self._resolve_project_file(base, rel_path)
        if target is None:
            raise ValueError("invalid file path")
        rel_parts = target.relative_to(base).parts
        if any(part in self._ARTIFACT_SKIP_DIRS for part in rel_parts):
            raise ValueError("cannot write to this path")
        if target.exists():
            raise ValueError("file already exists")
        data = content.encode("utf-8")
        if len(data) > self._ARTIFACT_MAX_BYTES:
            raise ValueError(f"file exceeds {self._ARTIFACT_MAX_BYTES // 1024} KB limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        log.info("artifact created %s", kv(project=project_slug, path=rel_path, bytes=len(data)))
        return {
            "path": target.relative_to(base).as_posix(),
            "size": len(data),
            "created": True,
        }

    _CONSOLE_TIMEOUT_S = 600
    _CONSOLE_MAX_OUTPUT = 2 * 1024 * 1024

    def project_workspace_cwd(self, project_slug: str) -> str:
        if not self.db.get_project(project_slug):
            raise ValueError("unknown project")
        return str(self._ensure_project_dir(project_slug).resolve())

    def run_workspace_command(self, project_slug: str, command: str) -> dict:
        """Run a shell command in the project's artifact workspace directory."""
        command = (command or "").strip()
        if not command:
            raise ValueError("command is required")
        if not self.db.get_project(project_slug):
            raise ValueError("unknown project")
        cwd = self._ensure_project_dir(project_slug)
        env = pi_subprocess_env()
        env.setdefault("TERM", "xterm-256color")
        env.pop("CI", None)
        timed_out = False
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self._CONSOLE_TIMEOUT_S,
                env=env,
            )
            exit_code = proc.returncode
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + "\n[command timed out]"
            if not isinstance(stdout, str):
                stdout = stdout.decode("utf-8", errors="replace") if stdout else ""
            if not isinstance(stderr, str):
                stderr = stderr.decode("utf-8", errors="replace") if stderr else ""
        truncated = False
        if len(stdout) + len(stderr) > self._CONSOLE_MAX_OUTPUT:
            truncated = True
            cap = self._CONSOLE_MAX_OUTPUT // 2
            if len(stdout) > cap:
                stdout = stdout[:cap] + "\n… (stdout truncated)"
            if len(stderr) > cap:
                stderr = stderr[:cap] + "\n… (stderr truncated)"
        log.info(
            "workspace console %s",
            kv(project=project_slug, exit=exit_code, timed_out=timed_out),
        )
        return {
            "project_slug": project_slug,
            "cwd": str(cwd.resolve()),
            "command": command,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "truncated": truncated,
        }

    def _resolve_project_file(self, base: Path, rel_path: str) -> Optional[Path]:
        if not rel_path or rel_path.strip() in (".", ".."):
            return None
        target = (base / rel_path).resolve()
        if target != base and base not in target.parents:
            return None
        return target

    def _on_orchestrate(self, project_slug: str, iteration_id: str, generation: Optional[int] = None) -> None:
        if generation is not None and generation != self._orchestrate_generation:
            log.info("orchestrate ignored after stop %s", kv(project=project_slug, iteration=iteration_id))
            return
        if not self.db.get_project(project_slug):
            log.error("orchestrate failed: unknown project %s", project_slug)
            return
        try:
            self._validate_orchestrate_target(project_slug, iteration_id)
        except ValueError as exc:
            log.error("orchestrate rejected: %s", exc)
            return
        from db import ITERATION_COMPLETED, ITERATION_RUNNING

        iteration = self.db.get_iteration(iteration_id) or {}
        self._skip_testing_for_run = iteration.get("state") == ITERATION_COMPLETED
        if not self._skip_testing_for_run:
            self.testing_status = "standby"
            self.testing_last_event = None
            self.testing_last_message = None

        # Clean switch: stop anything currently running before changing project.
        self._stop_all_work()
        self.active_project_slug = project_slug
        self.active_iteration_id = iteration_id
        self.tracker = LocalTracker(self.db, project_slug, iteration_id)
        self.orchestrating = True
        self.db.set_iteration_state(iteration_id, ITERATION_RUNNING)
        self._work_since_commit = 0
        self._completed_since_commit = []
        from workspace import WorkspaceManager

        try:
            project_dir = self._ensure_project_dir(project_slug)
        except Exception as exc:
            project_dir = None
            log.warning("could not create project workspace dir: %s", exc)
        if project_dir is not None and self.db.project_needs_git(project_slug):
            self._git_init(project_dir)
        log.info("orchestrating %s", kv(project=project_slug, iteration=iteration_id))
        self._repair_plan_deps(project_slug, iteration_id)
        self._events.put(("tick",))

    def _repair_plan_deps(self, project_slug: str, iteration_id: Optional[str] = None) -> None:
        """Remove inverted build→test edges left by bad planner output."""
        try:
            tasks = self.db.tasks_for_project(project_slug, iteration_id=iteration_id)
        except Exception:
            return
        test_ids = {t["id"] for t in tasks if (t.get("role") or "").lower() == "test"}
        if not test_ids:
            return
        for task in tasks:
            if (task.get("role") or "build").lower() != "build":
                continue
            for kind in ("depends", "tests"):
                for dep in self.db.deps_for_task(task["id"], kind=kind):
                    if dep["id"] in test_ids:
                        self.db.remove_task_dep(task["id"], dep["id"], kind)
                        log.info(
                            "removed inverted plan edge %s",
                            kv(build=task.get("identifier"), test=dep.get("identifier"), kind=kind),
                        )

    def _on_pause(self) -> dict:
        log.info("orchestration paused %s", kv(project=self.active_project_slug))
        self.orchestrating = False
        self.planning = False
        skip_testing = self._skip_testing_for_run
        iteration_ids: list[str] = []
        for iid in (self.active_iteration_id, self.planning_iteration_id):
            if iid and iid not in iteration_ids:
                iteration_ids.append(iid)
        self.planning_project_slug = None
        self.planning_iteration_id = None
        self._stop_all_work()
        updated_iteration = None
        for iteration_id in iteration_ids:
            iteration = self._reconcile_iteration_after_pause(iteration_id, skip_testing)
            if iteration:
                updated_iteration = iteration
        self.active_iteration_id = None
        self.testing_status = "idle"
        self.testing_last_event = None
        self.testing_last_message = None
        self._skip_testing_for_run = False
        result: dict = {"orchestrating": False}
        if updated_iteration:
            result["iteration"] = updated_iteration
            result["iteration_id"] = updated_iteration.get("id")
        elif iteration_ids:
            result["iteration_id"] = iteration_ids[0]
        return result

    def _stop_all_work(self) -> None:
        for issue_id in list(self.state.running.keys()):
            entry = self.state.running.pop(issue_id)
            self._add_runtime_seconds(entry)
            entry.worker.terminate()
            self._finalize_run(entry, "canceled")
        for retry in list(self.state.retry_attempts.values()):
            retry.timer.cancel()
        self.state.retry_attempts.clear()
        self.state.claimed.clear()

    # --- tick / dispatch ---
    def _on_tick(self) -> None:
        if not self.orchestrating or not self.tracker:
            return
        self.cm.maybe_reload()
        self._reconcile()

        config = self.cm.current
        errors = config.validate_for_dispatch()
        if errors:
            log.error("dispatch skipped, validation: %s", "; ".join(errors))
            return

        try:
            issues = self.tracker.fetch_candidate_issues(config.active_states())
        except TrackerError as exc:
            log.error("candidate fetch failed: %s", exc)
            return

        for issue in self._sort_for_dispatch(issues):
            if self._available_global_slots() <= 0:
                break
            if self._should_dispatch(issue):
                self._dispatch_issue(issue, attempt=None)

        # End of a drained sprint: optionally commit the project's Git repo.
        self._maybe_sprint_commit()
        self._maybe_dispatch_testing()
        self._refresh_testing_status()

    def _sort_for_dispatch(self, issues: list[Issue]) -> list[Issue]:
        def key(issue: Issue):
            order = issue.plan_order if isinstance(issue.plan_order, int) else 9999
            priority = issue.priority if isinstance(issue.priority, int) else 9999
            created = issue.created_at or "9999"
            return (order, priority, created, issue.identifier or "")

        return sorted(issues, key=key)

    def _available_global_slots(self) -> int:
        # Agents share one project workspace, so run one at a time.
        limit = 1
        return max(limit - len(self.state.running), 0)

    def _state_slot_available(self, state_name: str) -> bool:
        config = self.cm.current
        limit = config.max_concurrent_agents_by_state().get(state_name.strip().lower())
        if limit is None:
            return True
        current = sum(
            1 for e in self.state.running.values() if e.issue.state_lower() == state_name.strip().lower()
        )
        return current < limit

    def _terminal_states_lower(self) -> set[str]:
        return {s.strip().lower() for s in self.cm.current.terminal_states()}

    def _is_blocker_ready(self, blocker_state: Optional[str]) -> bool:
        """A dependency is satisfied once it reaches a terminal state (Completed/Cancelled)."""
        state = (blocker_state or "").strip().lower()
        return state in self._terminal_states_lower()

    def _should_dispatch(self, issue: Issue) -> bool:
        config = self.cm.current
        if not (issue.id and issue.identifier and issue.title and issue.state):
            return False
        active = [s.lower() for s in config.active_states()]
        terminal = self._terminal_states_lower()
        if issue.state_lower() not in active or issue.state_lower() in terminal:
            return False
        required = set(config.required_labels())
        if required and not required.issubset(set(issue.labels)):
            return False
        if issue.id in self.state.running or issue.id in self.state.claimed:
            return False
        if not self._state_slot_available(issue.state):
            return False
        if issue.state_lower() == STATE_TODO.lower():
            for blocker in issue.blocked_by:
                if not self._is_blocker_ready(blocker.state):
                    return False
        return True

    def _dispatch_issue(self, issue: Issue, attempt: Optional[int]) -> None:
        client = self.tracker
        if not client:
            return
        config = self.cm.current
        from workspace import WorkspaceManager

        workspaces = WorkspaceManager(lambda: self.cm.current)

        def emit_update(update: dict, issue_id=issue.id):
            self._events.put(("codex_update", issue_id, update))

        def emit_exit(reason: str, error, verdict=None, issue_id=issue.id):
            self._events.put(("worker_exit", issue_id, reason, error, verdict))

        project_slug = self.active_project_slug
        run_id: Optional[str] = None
        try:
            run_id = self.db.create_run(
                task_id=issue.id,
                identifier=issue.identifier,
                project_slug=project_slug,
                attempt=attempt or 0,
            )
        except Exception as exc:
            log.warning("could not record run for %s: %s", issue.identifier, exc)

        from workspace import sanitize_key, snapshot_workspace

        session_id = (
            f"{sanitize_key(issue.identifier)}-{run_id[:8]}"
            if run_id
            else sanitize_key(issue.identifier)
        )

        worker = AgentWorker(
            issue=issue,
            attempt=attempt,
            config=config,
            tracker=client,
            workspaces=workspaces,
            emit_update=emit_update,
            emit_exit=emit_exit,
            project_slug=self.active_project_slug,
            project_workspace_path=self._project_workspace_path(self.active_project_slug)
            if self.active_project_slug
            else None,
            single_turn=True,
            shared_workspace=True,
            pi_command=self.pi_command(),
            model_ref=self.pi_model_ref(),
            session_id=session_id,
        )
        entry = RunningEntry(
            worker=worker,
            issue=issue,
            identifier=issue.identifier,
            started_monotonic=time.monotonic(),
            retry_attempt=attempt or 0,
            started_at_iso=datetime.now(timezone.utc).isoformat(),
            last_activity_monotonic=time.monotonic(),
            run_id=run_id,
        )
        if self.active_project_slug:
            ws_path = workspaces.project_dir(
                self.active_project_slug,
                self._project_workspace_path(self.active_project_slug),
            )
            entry.workspace_path = ws_path
            entry.workspace_snapshot = snapshot_workspace(ws_path)
        self.state.running[issue.id] = entry
        self.state.claimed.add(issue.id)
        self._cancel_retry(issue.id)
        worker.start()
        log.info("dispatch %s", kv(issue_id=issue.id, issue_identifier=issue.identifier, attempt=attempt))

        try:
            client.set_issue_state(issue.id, STATE_IN_PROGRESS)
            entry.issue.state = STATE_IN_PROGRESS
        except TrackerError as exc:
            log.warning("could not update board state for %s: %s", issue.identifier, exc)
        if config.comment_on_start():
            self._safe_comment(client, issue.id, "Flight Deck started working on this issue.")

    # --- reconciliation (SPEC 8.5) ---
    def _reconcile(self) -> None:
        self._reconcile_stalled()
        running_ids = list(self.state.running.keys())
        if not running_ids:
            return
        client = self.tracker
        if not client:
            return
        try:
            refreshed = client.fetch_issue_states_by_ids(running_ids)
        except TrackerError as exc:
            log.debug("state refresh failed, keeping workers: %s", exc)
            return

        config = self.cm.current
        active = [s.lower() for s in config.active_states()]
        terminal = [s.lower() for s in config.terminal_states()]
        by_id = {issue.id: issue for issue in refreshed}
        for issue_id in running_ids:
            if issue_id == TESTING_AGENT_ID:
                continue
            issue = by_id.get(issue_id)
            if issue is None:
                continue
            if issue.state_lower() in terminal:
                self._terminate_running(issue_id, cleanup_workspace=True, release=True)
            elif issue.state_lower() in active:
                self.state.running[issue_id].issue.state = issue.state
            else:
                self._terminate_running(issue_id, cleanup_workspace=False, release=True)

    def _reconcile_stalled(self) -> None:
        config = self.cm.current
        stall_ms = config.stall_timeout_ms()
        if stall_ms <= 0:
            return
        now = time.monotonic()
        for issue_id in list(self.state.running.keys()):
            entry = self.state.running[issue_id]
            elapsed_ms = (now - entry.last_activity_monotonic) * 1000.0
            if elapsed_ms > stall_ms:
                log.warning("stall detected %s", kv(issue_identifier=entry.identifier, elapsed_ms=int(elapsed_ms)))
                self._add_runtime_seconds(entry)
                entry.worker.terminate()
                self.state.running.pop(issue_id, None)
                self._finalize_run(entry, "failed", error="stall")
                self._schedule_retry(issue_id, entry.retry_attempt + 1, entry.identifier, error="stall")

    def _terminate_running(self, issue_id: str, cleanup_workspace: bool, release: bool) -> None:
        entry = self.state.running.pop(issue_id, None)
        if entry is None:
            return
        self._add_runtime_seconds(entry)
        entry.worker.terminate()
        self._finalize_run(entry, "completed" if cleanup_workspace else "stopped")
        log.info(
            "terminate running %s",
            kv(issue_identifier=entry.identifier, cleanup=cleanup_workspace),
        )
        if release:
            self.state.claimed.discard(issue_id)

    # --- worker events ---
    def _on_codex_update(self, issue_id: str, update: dict) -> None:
        entry = self.state.running.get(issue_id)
        if entry is None:
            return
        entry.last_activity_monotonic = time.monotonic()
        event = update.get("event")
        # usage_sample is a background token poll; it keeps the agent "alive" for
        # stall detection and updates tokens, but must not overwrite the visible
        # last event (e.g. "thinking", "responding").
        if event and event != "usage_sample":
            entry.last_event = event
            entry.last_timestamp = update.get("timestamp")
            if entry.is_testing:
                self.testing_last_event = event
        if update.get("codex_app_server_pid"):
            entry.codex_app_server_pid = update["codex_app_server_pid"]
        if update.get("thread_id"):
            entry.thread_id = update["thread_id"]
        if event == "session_started":
            entry.session_id = f"{entry.thread_id}-0"
        if event == "turn_completed":
            entry.turn_count += 1
            entry.session_id = f"{entry.thread_id}-{entry.turn_count}"
        usage = update.get("usage")
        if isinstance(usage, dict):
            self._accumulate_tokens(entry, usage)

    def _accumulate_tokens(self, entry: RunningEntry, usage: dict) -> None:
        new_in = int(usage.get("input") or 0)
        new_out = int(usage.get("output") or 0)
        new_total = int(usage.get("total") or (new_in + new_out))
        self.state.totals.input_tokens += max(new_in - entry.last_reported_input, 0)
        self.state.totals.output_tokens += max(new_out - entry.last_reported_output, 0)
        self.state.totals.total_tokens += max(new_total - entry.last_reported_total, 0)
        entry.last_reported_input = new_in
        entry.last_reported_output = new_out
        entry.last_reported_total = new_total

    def _on_worker_exit(
        self, issue_id: str, reason: str, error: Optional[str], verdict: Optional[dict] = None
    ) -> None:
        entry = self.state.running.pop(issue_id, None)
        if entry is None:
            return  # already terminated by reconciliation/stall
        self._add_runtime_seconds(entry)
        log.info(
            "worker exit %s",
            kv(
                issue_id=issue_id,
                issue_identifier=entry.identifier,
                reason=reason,
                error=error,
                verdict=(verdict or {}).get("status"),
            ),
        )
        if reason == "normal":
            if entry.is_testing:
                self._on_testing_finish(entry, verdict)
            else:
                self._on_local_finish(entry, verdict)
        elif reason == "canceled":
            self._finalize_run(entry, "canceled")
            self.state.claimed.discard(issue_id)
        else:
            self._finalize_run(entry, "failed", error=error)
            self._schedule_retry(issue_id, entry.retry_attempt + 1, entry.identifier, error=error)

    def _on_local_finish(self, entry: RunningEntry, verdict: Optional[dict]) -> None:
        """Built-in tracker: PASS → Completed, FAIL → retry in In Progress."""
        issue_id = entry.issue.id
        client = self.tracker
        status = (verdict or {}).get("status", "").upper()
        reason = (verdict or {}).get("reason") or ""

        if status == "FAIL":
            log.info("task reported FAIL, retrying %s", kv(issue_identifier=entry.identifier))
            self._finalize_run(entry, "failed", error=reason or "task reported FAIL")
            self._schedule_retry(issue_id, entry.retry_attempt + 1, entry.identifier, error=reason or "FAIL")
            return

        if status != "PASS":
            log.info(
                "task missing PASS verdict, retrying %s",
                kv(issue_identifier=entry.identifier, status=status or "(none)"),
            )
            self._finalize_run(entry, "failed", error=reason or "PASS verdict required")
            self._schedule_retry(
                issue_id, entry.retry_attempt + 1, entry.identifier, error=reason or "PASS required"
            )
            return

        reject = self._reject_pass_without_deliverables(entry, verdict)
        if reject:
            log.warning(
                "PASS rejected %s",
                kv(issue_identifier=entry.identifier, reason=reject),
            )
            self._finalize_run(entry, "failed", error=reject)
            self._schedule_retry(issue_id, entry.retry_attempt + 1, entry.identifier, error=reject)
            return

        if client:
            try:
                client.set_issue_state(issue_id, STATE_COMPLETED)
                entry.issue.state = STATE_COMPLETED
                log.info("task completed %s", kv(issue_identifier=entry.identifier))
            except TrackerError as exc:
                log.warning("could not move %s to Completed: %s", entry.identifier, exc)
        if client and self.cm.current.comment_on_finish():
            self._safe_comment(client, issue_id, f"Task finished: {reason or 'work completed'}.")
        self._finalize_run(entry, "completed")
        self.state.claimed.discard(issue_id)
        self.state.completed.add(issue_id)
        self._work_since_commit += 1
        self._completed_since_commit.append(
            {
                "identifier": entry.identifier,
                "title": entry.issue.title,
                "role": getattr(entry.issue, "role", None) or "build",
            }
        )
        if self.active_iteration_id:
            self._maybe_finish_iteration_after_build()
        self._events.put(("tick",))

    def _reject_pass_without_deliverables(self, entry: RunningEntry, verdict: Optional[dict]) -> Optional[str]:
        """Reject a PASS when the agent produced no on-disk artifacts (common false completion)."""
        if not entry.workspace_path:
            return None
        from workspace import snapshot_workspace, workspace_changed

        after = snapshot_workspace(entry.workspace_path)
        if workspace_changed(entry.workspace_snapshot, after):
            return None
        used_tools = bool((verdict or {}).get("used_tools"))
        if used_tools:
            return (
                "PASS rejected: tools ran but no files were created or modified in the "
                "project workspace — write deliverables to disk before reporting PASS"
            )
        return (
            "PASS rejected: no files were created or modified and no tools were used — "
            "do the work (search, write files, run commands) before reporting PASS"
        )

    # --- retry (SPEC 8.4) ---
    def _schedule_retry(
        self,
        issue_id: str,
        attempt: int,
        identifier: Optional[str],
        error: Optional[str] = None,
        continuation: bool = False,
    ) -> None:
        self._cancel_retry(issue_id)
        if continuation:
            delay_ms = _CONTINUATION_DELAY_MS
        else:
            cap = self.cm.current.max_retry_backoff_ms()
            delay_ms = min(_FAILURE_BASE_MS * (2 ** max(attempt - 1, 0)), cap)
        timer = threading.Timer(delay_ms / 1000.0, lambda: self._events.put(("retry", issue_id)))
        timer.daemon = True
        self.state.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id,
            identifier=identifier,
            attempt=attempt,
            due_at_monotonic=time.monotonic() + delay_ms / 1000.0,
            timer=timer,
            error=error,
            due_at_epoch=time.time() + delay_ms / 1000.0,
        )
        self.state.claimed.add(issue_id)
        timer.start()
        log.info(
            "retry scheduled %s",
            kv(
                issue_identifier=identifier,
                attempt=attempt,
                delay_ms=delay_ms,
                error=error,
                continuation=continuation,
            ),
        )

    def _cancel_retry(self, issue_id: str) -> None:
        existing = self.state.retry_attempts.pop(issue_id, None)
        if existing is not None:
            existing.timer.cancel()

    def _on_retry(self, issue_id: str) -> None:
        if issue_id == TESTING_AGENT_ID:
            retry = self.state.retry_attempts.pop(issue_id, None)
            self.state.claimed.discard(issue_id)
            if retry is None or not self.orchestrating or not self.active_iteration_id:
                return
            iteration = self.db.get_iteration(self.active_iteration_id) or {}
            instructions = (iteration.get("testing_instructions") or "").strip()
            if instructions:
                self._dispatch_testing_agent(instructions)
            return
        retry = self.state.retry_attempts.pop(issue_id, None)
        if retry is None:
            return
        client = self.tracker
        if not client or not self.orchestrating:
            self.state.claimed.discard(issue_id)
            return
        config = self.cm.current
        try:
            candidates = client.fetch_candidate_issues(config.active_states())
        except TrackerError as exc:
            self._schedule_retry(issue_id, retry.attempt + 1, retry.identifier, error=f"retry poll failed: {exc}")
            return
        issue = next((i for i in candidates if i.id == issue_id), None)
        if issue is None:
            self.state.claimed.discard(issue_id)
            return
        if not self._should_continue_dispatch(issue):
            self.state.claimed.discard(issue_id)
            return
        if self._available_global_slots() <= 0 or not self._state_slot_available(issue.state):
            self._schedule_retry(issue_id, retry.attempt + 1, issue.identifier, error="no available orchestrator slots")
            return
        self._dispatch_issue(issue, attempt=retry.attempt)

    def _should_continue_dispatch(self, issue: Issue) -> bool:
        """Like _should_dispatch but ignores the claim we already hold for this issue."""
        config = self.cm.current
        active = [s.lower() for s in config.active_states()]
        terminal = self._terminal_states_lower()
        if issue.state_lower() not in active or issue.state_lower() in terminal:
            return False
        required = set(config.required_labels())
        if required and not required.issubset(set(issue.labels)):
            return False
        if issue.id in self.state.running:
            return False
        if issue.state_lower() == STATE_TODO.lower():
            for blocker in issue.blocked_by:
                if not self._is_blocker_ready(blocker.state):
                    return False
        return True

    # --- helpers ---
    def _finalize_run(self, entry: RunningEntry, outcome: str, error: Optional[str] = None) -> None:
        if not entry.run_id:
            return
        runtime = max(time.monotonic() - entry.started_monotonic, 0.0)
        try:
            self.db.finalize_run(
                entry.run_id,
                outcome=outcome,
                input_tokens=entry.last_reported_input,
                output_tokens=entry.last_reported_output,
                total_tokens=entry.last_reported_total,
                turn_count=entry.turn_count,
                runtime_seconds=round(runtime, 1),
                session_id=entry.session_id,
                state=entry.issue.state,
                error=error,
            )
        except Exception as exc:
            log.warning("could not finalize run for %s: %s", entry.identifier, exc)

    def _add_runtime_seconds(self, entry: RunningEntry) -> None:
        self.state.totals.seconds_running += max(time.monotonic() - entry.started_monotonic, 0.0)

    def _safe_comment(self, client, issue_id: str, body: str) -> None:
        try:
            client.add_comment(issue_id, body)
        except TrackerError as exc:
            log.warning("comment failed for %s: %s", issue_id, exc)

    # --- per-project Git (local-only; never touches global config or a remote) ---
    _GITIGNORE_DEFAULT = (
        "node_modules/\n__pycache__/\n.venv/\n.pi/\n*.pyc\n.DS_Store\n"
    )

    def _git(self, repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _git_init(self, repo: Path) -> None:
        try:
            if not (repo / ".git").exists():
                self._git(repo, "init")
                # Repo-local identity only — we must never edit the user's global git config.
                self._git(repo, "config", "user.name", "Flight Deck")
                self._git(repo, "config", "user.email", "flightdeck@localhost")
                gitignore = repo / ".gitignore"
                if not gitignore.exists():
                    gitignore.write_text(self._GITIGNORE_DEFAULT, encoding="utf-8")
                log.info("git repo initialized %s", kv(repo=str(repo)))
        except Exception as exc:
            log.warning("git init failed for %s: %s", repo, exc)

    # --- adversarial testing agent (iteration QA gate) ---
    def _build_agents_active(self) -> bool:
        for entry in self.state.running.values():
            if not entry.is_testing:
                return True
        for retry in self.state.retry_attempts.values():
            if retry.issue_id == TESTING_AGENT_ID:
                continue
            return True
        return False

    def _testing_agent_running(self) -> bool:
        return TESTING_AGENT_ID in self.state.running

    def _refresh_testing_status(self) -> None:
        if not self.orchestrating or self._skip_testing_for_run:
            if not self._testing_agent_running():
                self.testing_status = "idle"
            return
        if self._testing_agent_running():
            self.testing_status = "running"
            return
        if self.active_iteration_id and self.db.iteration_tasks_all_terminal(self.active_iteration_id):
            if self._build_agents_active() or self.db.iteration_has_open_build_work(self.active_iteration_id):
                self.testing_status = "standby"
            else:
                self.testing_status = "standby"
        elif self.orchestrating:
            self.testing_status = "standby"

    def _maybe_dispatch_testing(self) -> None:
        if not self.orchestrating or self._skip_testing_for_run:
            return
        if not self.active_iteration_id or not self.active_project_slug:
            return
        if self._testing_agent_running() or self._build_agents_active():
            return
        if TESTING_AGENT_ID in self.state.retry_attempts or TESTING_AGENT_ID in self.state.claimed:
            return
        if not self.db.iteration_tasks_all_terminal(self.active_iteration_id):
            return
        if self.db.iteration_has_open_build_work(self.active_iteration_id):
            return
        from db import ITERATION_COMPLETED

        iteration = self.db.get_iteration(self.active_iteration_id)
        if not iteration or iteration.get("state") == ITERATION_COMPLETED:
            return
        instructions = (iteration.get("testing_instructions") or "").strip()
        if not instructions:
            log.error("testing skipped: no instructions on iteration %s", self.active_iteration_id)
            return
        self._dispatch_testing_agent(instructions)

    def _dispatch_testing_agent(self, instructions: str) -> None:
        from db import ITERATION_TESTING
        from testing_prompt import render_testing_prompt

        client = self.tracker
        if not client or not self.active_project_slug:
            return
        build_tasks = [
            t
            for t in self.db.iteration_build_tasks(self.active_iteration_id)
            if t.get("state") == STATE_COMPLETED
        ]
        prompt = render_testing_prompt(
            project_slug=self.active_project_slug,
            instructions=instructions,
            tasks=build_tasks,
        )
        issue = Issue(
            id=TESTING_AGENT_ID,
            identifier=TESTING_AGENT_IDENTIFIER,
            title="Quality Assurance Review",
            description=instructions,
            state="Testing",
            role="testing",
            agent_name="QA Agent",
            iteration_id=self.active_iteration_id,
        )
        config = self.cm.current
        from workspace import WorkspaceManager, snapshot_workspace

        workspaces = WorkspaceManager(lambda: self.cm.current)

        def emit_update(update: dict, issue_id=TESTING_AGENT_ID):
            self._events.put(("codex_update", issue_id, update))

        def emit_exit(reason: str, error, verdict=None, issue_id=TESTING_AGENT_ID):
            self._events.put(("worker_exit", issue_id, reason, error, verdict))

        run_id: Optional[str] = None
        try:
            run_id = self.db.create_run(
                task_id=None,
                identifier=TESTING_AGENT_IDENTIFIER,
                project_slug=self.active_project_slug,
                attempt=0,
            )
        except Exception as exc:
            log.warning("could not record testing run: %s", exc)

        session_id = f"qa-{run_id[:8]}" if run_id else "qa-agent"

        worker = AgentWorker(
            issue=issue,
            attempt=None,
            config=config,
            tracker=client,
            workspaces=workspaces,
            emit_update=emit_update,
            emit_exit=emit_exit,
            project_slug=self.active_project_slug,
            project_workspace_path=self._project_workspace_path(self.active_project_slug),
            single_turn=True,
            shared_workspace=True,
            pi_command=self.pi_command(),
            model_ref=self.pi_model_ref(),
            session_id=session_id,
            custom_prompt=prompt,
        )
        entry = RunningEntry(
            worker=worker,
            issue=issue,
            identifier=TESTING_AGENT_IDENTIFIER,
            started_monotonic=time.monotonic(),
            retry_attempt=0,
            started_at_iso=datetime.now(timezone.utc).isoformat(),
            last_activity_monotonic=time.monotonic(),
            run_id=run_id,
            is_testing=True,
        )
        ws_path = workspaces.project_dir(
            self.active_project_slug,
            self._project_workspace_path(self.active_project_slug),
        )
        entry.workspace_path = ws_path
        entry.workspace_snapshot = snapshot_workspace(ws_path)
        self.state.running[TESTING_AGENT_ID] = entry
        self.db.set_iteration_state(self.active_iteration_id, ITERATION_TESTING)
        self.testing_status = "running"
        self.testing_last_event = "starting"
        self.testing_last_message = "QA agent reviewing the sprint…"
        worker.start()
        log.info(
            "testing agent dispatched %s",
            kv(project=self.active_project_slug, iteration=self.active_iteration_id),
        )

    def _on_testing_finish(self, entry: RunningEntry, verdict: Optional[dict]) -> None:
        from db import ITERATION_COMPLETED, ITERATION_RUNNING

        status = (verdict or {}).get("status", "").upper()
        reason = (verdict or {}).get("reason") or ""
        self.testing_last_message = reason or None

        if status == "PASS":
            self._finalize_run(entry, "completed")
            self.testing_status = "passed"
            self.testing_last_event = "passed"
            if self.active_iteration_id:
                self.db.set_iteration_state(self.active_iteration_id, ITERATION_RUNNING)
                if self.db.maybe_complete_iteration(self.active_iteration_id):
                    log.info("iteration completed after QA pass %s", kv(iteration=self.active_iteration_id))
            self.orchestrating = False
            self._events.put(("tick",))
            return

        reopen = [ident for ident in (verdict or {}).get("reopen") or [] if ident]
        if status == "FAIL" and reopen:
            reopened = self._reopen_tasks(reopen)
            self._finalize_run(entry, "failed", error=reason or "QA reported issues")
            self.testing_status = "standby"
            self.testing_last_event = "reopened"
            if self.active_iteration_id:
                self.db.set_iteration_state(self.active_iteration_id, ITERATION_RUNNING)
            log.info(
                "testing reopened tasks %s",
                kv(project=self.active_project_slug, tasks=reopened),
            )
            self._events.put(("tick",))
            return

        err = reason or "QA agent did not report PASS"
        log.info("testing inconclusive, retrying %s", kv(reason=err, status=status or "(none)"))
        self._finalize_run(entry, "failed", error=err)
        self.testing_status = "standby"
        self.testing_last_event = "retry"
        if self.active_iteration_id:
            self.db.set_iteration_state(self.active_iteration_id, ITERATION_RUNNING)
        self._schedule_retry(
            TESTING_AGENT_ID,
            entry.retry_attempt + 1,
            TESTING_AGENT_IDENTIFIER,
            error=err,
        )

    def _reopen_tasks(self, identifiers: list[str]) -> list[str]:
        client = self.tracker
        if not client or not self.active_project_slug:
            return []
        reopened: list[str] = []
        for ident in identifiers:
            task = self.db.get_task_by_identifier(self.active_project_slug, ident)
            if not task:
                log.warning("QA reopen: unknown task %s", ident)
                continue
            if self.active_iteration_id and task.get("iteration_id") != self.active_iteration_id:
                log.warning("QA reopen: task %s not in active iteration", ident)
                continue
            try:
                client.set_issue_state(task["id"], STATE_IN_PROGRESS)
                reopened.append(ident)
            except TrackerError as exc:
                log.warning("QA reopen failed for %s: %s", ident, exc)
        return reopened

    def _maybe_finish_iteration_after_build(self) -> None:
        if not self.active_iteration_id:
            return
        if self._skip_testing_for_run:
            if self.db.maybe_complete_iteration(self.active_iteration_id):
                log.info("iteration completed %s", kv(iteration=self.active_iteration_id))
            return
        self._events.put(("tick",))

    def _maybe_sprint_commit(self) -> None:
        if not self.orchestrating:
            return
        slug = self.active_project_slug
        if not slug or not self.db.project_needs_git(slug):
            return
        # Only commit once the sprint has fully drained and real work happened.
        if self.state.running or self.state.retry_attempts:
            return
        if self._work_since_commit <= 0:
            return
        n = self._work_since_commit
        completed = list(self._completed_since_commit)
        self._work_since_commit = 0
        self._completed_since_commit = []
        try:
            repo = self._project_base(slug)
            if not (repo / ".git").exists():
                self._git_init(repo)
            status = self._git(repo, "status", "--porcelain")
            if not (status.stdout or "").strip():
                log.info("git: nothing to commit for %s", slug)
                return
            self._git(repo, "add", "-A")
            import naming

            project = self.db.get_project(slug) or {}
            changed = naming.list_staged_files(repo)
            msg = naming.suggest_commit_message(
                self.cm.current,
                repo,
                slug,
                project.get("title") or slug,
                completed,
                changed,
                pi_command=self.pi_command(),
                model_ref=self.pi_model_ref(),
            )
            if not msg:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                titles = ", ".join(t.get("title", "?") for t in completed[:3])
                if len(completed) > 3:
                    titles += f" (+{len(completed) - 3} more)"
                msg = f"feat: complete {n} task(s) — {titles} ({ts})"
            result = self._git(repo, "commit", "-m", msg)
            if result.returncode == 0:
                log.info("git commit %s", kv(project=slug, tasks=n))
            else:
                log.warning("git commit failed for %s: %s", slug, (result.stderr or "").strip()[:200])
        except Exception as exc:
            log.warning("git commit error for %s: %s", slug, exc)

    def list_commits(self, project_slug: str, limit: int = 100) -> list[dict]:
        """Parse `git log` for a project's repo. Returns [] if it isn't a repo."""
        repo = self._project_base(project_slug)
        if not (repo / ".git").exists():
            return []
        sep = "\x1f"  # unit separator, unlikely to appear in commit fields
        fmt = sep.join(["%H", "%an", "%aI", "%s"])
        try:
            result = self._git(repo, "log", f"--pretty=format:{fmt}", f"-n{int(limit)}")
        except Exception as exc:
            log.warning("git log failed for %s: %s", project_slug, exc)
            return []
        if result.returncode != 0:
            return []
        commits: list[dict] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split(sep)
            if len(parts) == 4:
                commits.append(
                    {"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}
                )
        return commits

    # --- observability API (consumed by the HTTP dashboard, SPEC 13.7) ---
    def request_refresh(self) -> None:
        self._events.put(("tick",))

    def _live_seconds(self) -> float:
        now = time.monotonic()
        active = sum(now - e.started_monotonic for e in list(self.state.running.values()))
        return self.state.totals.seconds_running + active

    def _project_totals(self, project_slug: str) -> dict:
        """Historical run totals for a project, plus in-flight tokens/runtime when it is orchestrating."""
        totals = self.db.project_run_totals(project_slug)
        if self.orchestrating and self.active_project_slug == project_slug:
            now = time.monotonic()
            for entry in self.state.running.values():
                totals["input_tokens"] += entry.last_reported_input
                totals["output_tokens"] += entry.last_reported_output
                totals["total_tokens"] += entry.last_reported_total
                totals["seconds_running"] += max(now - entry.started_monotonic, 0.0)
            totals["seconds_running"] = round(totals["seconds_running"], 1)
        return totals

    def _running_row(self, entry: RunningEntry) -> dict:
        return {
            "issue_id": entry.issue.id,
            "issue_identifier": entry.identifier,
            "issue_url": entry.issue.url,
            "agent_name": getattr(entry.issue, "agent_name", None),
            "role": getattr(entry.issue, "role", None),
            "state": entry.issue.state,
            "session_id": entry.session_id,
            "turn_count": entry.turn_count,
            "last_event": entry.last_event,
            "last_message": entry.last_message or "",
            "started_at": entry.started_at_iso,
            "last_event_at": entry.last_timestamp,
            "tokens": {
                "input_tokens": entry.last_reported_input,
                "output_tokens": entry.last_reported_output,
                "total_tokens": entry.last_reported_total,
            },
        }

    def _retry_row(self, retry: RetryEntry) -> dict:
        due_at = None
        if retry.due_at_epoch:
            due_at = datetime.fromtimestamp(retry.due_at_epoch, timezone.utc).isoformat()
        return {
            "issue_id": retry.issue_id,
            "issue_identifier": retry.identifier,
            "attempt": retry.attempt,
            "due_at": due_at,
            "error": retry.error,
        }

    def _testing_api_state(self) -> dict:
        entry = self.state.running.get(TESTING_AGENT_ID)
        return {
            "status": self.testing_status,
            "last_event": self.testing_last_event,
            "last_message": self.testing_last_message,
            "active": bool(entry and entry.is_testing),
            "identifier": TESTING_AGENT_IDENTIFIER,
        }

    def api_state(self, project_slug: Optional[str] = None) -> dict:
        self.reconcile_stuck_iterations()
        running = list(self.state.running.values())
        retrying = list(self.state.retry_attempts.values())
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "orchestrating": self.orchestrating,
            "planning": self.planning,
            "planning_project": self.planning_project_slug,
            "planning_iteration": getattr(self, "planning_iteration_id", None),
            "active_project": self.active_project_slug,
            "active_iteration": self.active_iteration_id,
            "counts": {"running": len(running), "retrying": len(retrying)},
            "running": [self._running_row(e) for e in running],
            "retrying": [self._retry_row(r) for r in retrying],
            "codex_totals": {
                "input_tokens": self.state.totals.input_tokens,
                "output_tokens": self.state.totals.output_tokens,
                "total_tokens": self.state.totals.total_tokens,
                "seconds_running": round(self._live_seconds(), 1),
            },
            "rate_limits": None,
            "model_setup": self.model_settings.to_api(),
            "testing": self._testing_api_state(),
        }
        slug = (project_slug or "").strip()
        if slug:
            result["project_totals"] = self._project_totals(slug)
        return result

    def api_issue(self, identifier: str) -> Optional[dict]:
        ident = identifier.strip().lower()
        for entry in list(self.state.running.values()):
            if (entry.identifier or "").lower() == ident:
                return {
                    "issue_identifier": entry.identifier,
                    "issue_id": entry.issue.id,
                    "status": "running",
                    "running": self._running_row(entry),
                    "retry": None,
                }
        for retry in list(self.state.retry_attempts.values()):
            if (retry.identifier or "").lower() == ident:
                return {
                    "issue_identifier": retry.identifier,
                    "issue_id": retry.issue_id,
                    "status": "retrying",
                    "running": None,
                    "retry": self._retry_row(retry),
                }
        return None
