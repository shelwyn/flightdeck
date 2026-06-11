from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from config import Config
from logging_setup import get_logger, kv

log = get_logger("orch.workspace")

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_key(identifier: str) -> str:
    return _SANITIZE_RE.sub("_", identifier or "")


@dataclass
class Workspace:
    path: Path
    workspace_key: str
    created_now: bool


class WorkspaceError(Exception):
    pass


_SKIP_DIR_NAMES = frozenset({".git", ".pi", "__pycache__", ".venv", "node_modules"})


def snapshot_workspace(path: Path) -> dict[str, float]:
    """Map relative file path -> mtime for deliverable checks after an agent run."""
    if not path.is_dir():
        return {}
    out: dict[str, float] = {}
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        if _SKIP_DIR_NAMES.intersection(p.parts):
            continue
        out[str(p.relative_to(path))] = p.stat().st_mtime
    return out


def workspace_changed(before: dict[str, float], after: dict[str, float]) -> bool:
    return before != after


class WorkspaceManager:
    """Per-issue workspace lifecycle + hooks + path-safety invariants (SPEC 9)."""

    def __init__(self, get_config: Callable[[], Config]):
        self._get_config = get_config

    def _root(self) -> Path:
        root = self._get_config().workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def project_dir(self, project_slug: Optional[str], workspace_path: Optional[str] = None) -> Path:
        if workspace_path:
            path = Path(workspace_path).expanduser().resolve()
            if not path.is_dir():
                raise WorkspaceError(f"workspace path is not a directory: {path}")
            return path
        root = self._root()
        if not project_slug:
            return root
        return (root / sanitize_key(project_slug)).resolve()

    def ensure_project_dir(self, project_slug: str, workspace_path: Optional[str] = None) -> Path:
        if workspace_path:
            return self.project_dir(project_slug, workspace_path)
        path = self.project_dir(project_slug)
        root = self._root()
        if root != path and root not in path.parents:
            raise WorkspaceError(f"project dir {path} escapes root {root}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, identifier: str, project_slug: Optional[str] = None, workspace_path: Optional[str] = None) -> Workspace:
        root = self._root()
        base = self.project_dir(project_slug, workspace_path)
        key = sanitize_key(identifier)
        path = (base / key).resolve()
        # Per-issue dirs must stay inside the project workspace (custom or default).
        if base != path and base not in path.parents:
            raise WorkspaceError(f"workspace path {path} escapes project dir {base}")
        created_now = not path.exists()
        return Workspace(path=path, workspace_key=key, created_now=created_now)

    def project_workspace(self, project_slug: str, workspace_path: Optional[str] = None) -> Workspace:
        """Shared workspace for all tasks of a project (built-in tracker).

        All of a project's agents run in this one directory so they build on each
        other's files; it is also the Git repo root when the project enables Git.
        """
        custom = bool(workspace_path)
        path = self.project_dir(project_slug, workspace_path)
        if not custom:
            root = self._root()
            if root != path and root not in path.parents:
                raise WorkspaceError(f"project dir {path} escapes root {root}")
        created_now = not path.exists()
        if not custom:
            path.mkdir(parents=True, exist_ok=True)
        elif not path.is_dir():
            raise WorkspaceError(f"workspace path is not a directory: {path}")
        if created_now and not custom:
            self.run_hook("after_create", path, fatal=True)
        return Workspace(path=path, workspace_key=sanitize_key(project_slug), created_now=created_now)

    def create_for_issue(
        self, identifier: str, project_slug: Optional[str] = None, workspace_path: Optional[str] = None
    ) -> Workspace:
        workspace = self.path_for(identifier, project_slug, workspace_path)
        if workspace.path.exists() and not workspace.path.is_dir():
            raise WorkspaceError(f"workspace path exists but is not a directory: {workspace.path}")
        workspace.path.mkdir(parents=True, exist_ok=True)
        if workspace.created_now:
            self.run_hook("after_create", workspace.path, fatal=True)
        return workspace

    def cleanup_for_issue(self, identifier: str, project_slug: Optional[str] = None) -> None:
        workspace = self.path_for(identifier, project_slug)
        if not workspace.path.exists():
            return
        self.run_hook("before_remove", workspace.path, fatal=False)
        shutil.rmtree(workspace.path, ignore_errors=True)
        log.info("workspace removed %s", kv(workspace=workspace.path))

    def run_hook(self, name: str, cwd: Path, fatal: bool) -> None:
        config = self._get_config()
        script = config.hook(name)
        if not script:
            return
        timeout = config.hook_timeout_ms() / 1000.0
        log.info("hook start %s", kv(hook=name, cwd=cwd))
        try:
            result = subprocess.run(
                ["bash", "-lc", script],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log.error("hook timeout %s", kv(hook=name))
            if fatal:
                raise WorkspaceError(f"{name} hook timed out")
            return
        if result.returncode != 0:
            log.error("hook failed %s", kv(hook=name, code=result.returncode))
            if fatal:
                raise WorkspaceError(f"{name} hook failed: {result.stderr[:300]}")
