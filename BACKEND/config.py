from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from logging_setup import get_logger
from workflow import WorkflowDefinition, WorkflowError, load_workflow

log = get_logger("orch.config")


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Config:
    """Typed view over WORKFLOW.md front matter with defaults and resolution."""

    def __init__(self, definition: WorkflowDefinition):
        self.definition = definition
        self.raw = definition.config or {}
        self.workflow_dir = definition.dir
        self.prompt_template = definition.prompt_template

    def _section(self, name: str) -> dict:
        section = self.raw.get(name)
        return section if isinstance(section, dict) else {}

    # --- tracker ---
    def required_labels(self) -> list[str]:
        labels = self._section("tracker").get("required_labels") or []
        if not isinstance(labels, list):
            return []
        return [str(item).strip().lower() for item in labels]

    def active_states(self) -> list[str]:
        states = self._section("tracker").get("active_states")
        if not states:
            return ["Todo", "In Progress"]
        return [str(s) for s in states]

    def terminal_states(self) -> list[str]:
        states = self._section("tracker").get("terminal_states")
        if not states:
            return ["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]
        return [str(s) for s in states]

    def comment_on_start(self) -> bool:
        return bool(self._section("tracker").get("comment_on_start", False))

    def comment_on_finish(self) -> bool:
        return bool(self._section("tracker").get("comment_on_finish", False))

    # --- polling ---
    def poll_interval_ms(self) -> int:
        return _as_int(self._section("polling").get("interval_ms"), 30000)

    # --- workspace ---
    def workspace_root(self) -> Path:
        raw = self._section("workspace").get("root")
        value = str(raw) if raw else os.path.join(tempfile.gettempdir(), "symphony_workspaces")
        value = os.path.expandvars(value)
        value = os.path.expanduser(value)
        path = Path(value)
        if not path.is_absolute():
            path = self.workflow_dir / path
        return path.resolve()

    # --- hooks ---
    def hook(self, name: str) -> Optional[str]:
        value = self._section("hooks").get(name)
        return str(value) if value else None

    def hook_timeout_ms(self) -> int:
        return _as_int(self._section("hooks").get("timeout_ms"), 60000)

    # --- agent ---
    def max_concurrent_agents(self) -> int:
        return _as_int(self._section("agent").get("max_concurrent_agents"), 10)

    def max_turns(self) -> int:
        return max(1, _as_int(self._section("agent").get("max_turns"), 20))

    def max_retry_backoff_ms(self) -> int:
        return _as_int(self._section("agent").get("max_retry_backoff_ms"), 300000)

    def max_concurrent_agents_by_state(self) -> dict[str, int]:
        raw = self._section("agent").get("max_concurrent_agents_by_state") or {}
        result: dict[str, int] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                count = _as_int(value, 0)
                if count > 0:
                    result[str(key).strip().lower()] = count
        return result

    # --- codex / agent app-server (Pi RPC) ---
    def codex_command(self) -> str:
        return str(self._section("codex").get("command") or "pi --mode rpc")

    def turn_timeout_ms(self) -> int:
        return _as_int(self._section("codex").get("turn_timeout_ms"), 3600000)

    def read_timeout_ms(self) -> int:
        return _as_int(self._section("codex").get("read_timeout_ms"), 5000)

    def stall_timeout_ms(self) -> int:
        return _as_int(self._section("codex").get("stall_timeout_ms"), 300000)

    # --- server (HTTP dashboard extension, SPEC 13.7) ---
    def server_port(self) -> Optional[int]:
        port = self._section("server").get("port")
        if port is None:
            return None
        try:
            return int(port)
        except (TypeError, ValueError):
            return None

    def server_host(self) -> str:
        return str(self._section("server").get("host") or "127.0.0.1")

    # --- validation (SPEC 6.3 dispatch preflight) ---
    def validate_for_dispatch(self) -> list[str]:
        errors: list[str] = []
        if not self.codex_command().strip():
            errors.append("codex.command is empty")
        return errors


class ConfigManager:
    """Loads Config and reloads it when WORKFLOW.md changes, keeping last-known-good."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime: Optional[float] = None
        self._config = self._load()

    def _load(self) -> Config:
        definition = load_workflow(self.path)
        config = Config(definition)
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None
        return config

    @property
    def current(self) -> Config:
        return self._config

    def maybe_reload(self) -> bool:
        """Reload if the file changed. On failure keep last good config and log."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            log.warning("workflow stat failed; keeping last good config: %s", exc)
            return False
        if self._mtime is not None and mtime == self._mtime:
            return False
        try:
            new_config = self._load()
        except WorkflowError as exc:
            log.error("invalid workflow reload; keeping last good config: %s", exc)
            return False
        self._config = new_config
        log.info("workflow reloaded from %s", self.path)
        return True
