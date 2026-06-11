from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from config import Config
from tracker import TrackerError
from logging_setup import get_logger, kv
from models import Issue
from prompt import PromptError, render_prompt
from rpc_client import PiRpcClient, RpcError
from workspace import WorkspaceError, WorkspaceManager, sanitize_key

log = get_logger("orch.runner")

# message_update fires per token; we throttle it to a periodic heartbeat so the
# dashboard shows live activity AND the stall detector doesn't kill a healthy
# agent that is just thinking/streaming for a while.
_MIN_UPDATE_INTERVAL_S = 2.0
# Map the streaming sub-event to a short, human label shown as "last event".
_PHASE_LABELS = {
    "start": "starting",
    "thinking_start": "thinking",
    "thinking_delta": "thinking",
    "text_start": "responding",
    "text_delta": "responding",
    "toolcall_start": "preparing tool call",
    "toolcall_delta": "preparing tool call",
    "done": "message complete",
}
# How often to sample cumulative token usage while a turn is in flight.
_USAGE_SAMPLE_INTERVAL_S = 10.0

_CONTINUATION = (
    "Continue working on this same issue. Review the current state of the workspace, "
    "then proceed with the next step. If the issue is already fully resolved, say so."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_STATUS_RE = re.compile(r"^\s*STATUS\s*:\s*(PASS|FAIL)\b", re.IGNORECASE | re.MULTILINE)
_REOPEN_RE = re.compile(r"^\s*REOPEN\s*:\s*([A-Za-z0-9\-_,\s]+)$", re.IGNORECASE | re.MULTILINE)
_REASON_RE = re.compile(r"^\s*REASON\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_verdict(text: Optional[str]) -> Optional[dict]:
    """Parse the structured finish protocol from an agent's final message.

    Looks for lines like:
        STATUS: PASS|FAIL
        REOPEN: <identifier>[, <identifier>...]   (tests only, on FAIL)
        REASON: <free text>
    Returns None when no STATUS line is present.
    """
    if not text:
        return None
    m = _STATUS_RE.search(text)
    if not m:
        return None
    status = m.group(1).upper()
    reopen: list[str] = []
    rm = _REOPEN_RE.search(text)
    if rm:
        reopen = [tok.strip() for tok in re.split(r"[,\s]+", rm.group(1)) if tok.strip()]
    reason = None
    rsm = _REASON_RE.search(text)
    if rsm:
        reason = rsm.group(1).strip()
    return {"status": status, "reopen": reopen, "reason": reason}


class AgentWorker:
    """Runs one issue: workspace + Pi RPC session + bounded multi-turn loop (SPEC 16.5)."""

    def __init__(
        self,
        issue: Issue,
        attempt: Optional[int],
        config: Config,
        linear,
        workspaces: WorkspaceManager,
        emit_update: Callable[[dict], None],
        emit_exit: Callable[..., None],
        project_slug: Optional[str] = None,
        project_workspace_path: Optional[str] = None,
        single_turn: bool = False,
        shared_workspace: bool = False,
        pi_command: Optional[str] = None,
        model_ref: Optional[str] = None,
        session_id: Optional[str] = None,
        custom_prompt: Optional[str] = None,
    ):
        self.issue = issue
        self.attempt = attempt
        self.config = config
        self.pi_command = pi_command or config.codex_command()
        self.model_ref = (model_ref or "").strip() or None
        self.session_id = (session_id or "").strip() or sanitize_key(issue.identifier)
        self.custom_prompt = (custom_prompt or "").strip() or None
        self.linear = linear
        self.workspaces = workspaces
        self.emit_update = emit_update
        self.emit_exit = emit_exit
        self.project_slug = project_slug
        self.project_workspace_path = project_workspace_path
        self.single_turn = single_turn
        self.shared_workspace = shared_workspace

        self._rpc: Optional[PiRpcClient] = None
        self._terminated = threading.Event()
        self._finished = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_emit_monotonic = 0.0
        self._used_tools = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def terminate(self) -> None:
        self._terminated.set()
        self._finished.set()
        if self._rpc:
            self._rpc.terminate()

    def _on_event(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "message_update":
            sub = (obj.get("assistantMessageEvent") or {}).get("type") or ""
            if "toolcall" in sub:
                self._used_tools = True
            # Throttle the high-frequency token stream into a heartbeat.
            now = time.monotonic()
            if now - self._last_emit_monotonic < _MIN_UPDATE_INTERVAL_S:
                return
            self._last_emit_monotonic = now
            event = _PHASE_LABELS.get(sub, "streaming")
        elif kind and "tool" in kind:
            self._used_tools = True
            self._last_emit_monotonic = time.monotonic()
            event = kind
        else:
            self._last_emit_monotonic = time.monotonic()
            event = kind
        update = {"event": event, "timestamp": _now()}
        if self._rpc and self._rpc.pid:
            update["codex_app_server_pid"] = self._rpc.pid
        self.emit_update(update)

    def _usage_poll_loop(self) -> None:
        """Sample cumulative token usage while work is in flight so the dashboard
        shows tokens climbing instead of sitting at zero until the turn ends."""
        while not self._finished.wait(_USAGE_SAMPLE_INTERVAL_S):
            if self._terminated.is_set() or not self._rpc:
                return
            try:
                usage = self._rpc.get_token_usage()
            except Exception:
                usage = None
            if usage:
                self.emit_update({"event": "usage_sample", "timestamp": _now(), "usage": usage})

    def _run(self) -> None:
        ctx = kv(issue_id=self.issue.id, issue_identifier=self.issue.identifier)
        try:
            if self.shared_workspace and self.project_slug:
                # Built-in tracker: all of a project's tasks share one directory so
                # later tasks build on earlier ones (and Git tracks the whole project).
                workspace = self.workspaces.project_workspace(
                    self.project_slug, self.project_workspace_path
                )
            else:
                workspace = self.workspaces.create_for_issue(
                    self.issue.identifier, self.project_slug, self.project_workspace_path
                )
            self.workspaces.run_hook("before_run", workspace.path, fatal=True)

            agent_name = self.issue.agent_name or self.issue.identifier
            self._rpc = PiRpcClient(
                command=self.pi_command,
                cwd=workspace.path,
                session_id=self.session_id,
                session_name=f"{agent_name} · {self.issue.identifier}: {self.issue.title}",
                read_timeout_ms=self.config.read_timeout_ms(),
                on_event=self._on_event,
                model_ref=self.model_ref,
            )
            self._rpc.start()
            thread_id = self._rpc.get_session_id() or sanitize_key(self.issue.identifier)
            self.emit_update(
                {
                    "event": "session_started",
                    "timestamp": _now(),
                    "thread_id": thread_id,
                    "codex_app_server_pid": self._rpc.pid,
                }
            )
            threading.Thread(target=self._usage_poll_loop, daemon=True).start()

            max_turns = self.config.max_turns()
            turn_timeout = self.config.turn_timeout_ms()
            turn = 1
            while True:
                if self._terminated.is_set():
                    return self._finish(workspace, "canceled", None)

                if turn == 1:
                    if self.custom_prompt:
                        message = self.custom_prompt
                    else:
                        message = render_prompt(self.config.prompt_template, self.issue, self.attempt)
                else:
                    message = _CONTINUATION

                result = self._rpc.run_turn(message, turn_timeout)
                self._emit_turn_end(turn, result.reason)

                if self._terminated.is_set():
                    return self._finish(workspace, "canceled", None)
                if not result.ok:
                    log.warning(
                        "agent turn failed %s",
                        kv(
                            issue_identifier=self.issue.identifier,
                            attempt=self.attempt,
                            reason=result.reason,
                            stop=result.stop_reason,
                        ),
                    )
                    return self._finish(workspace, "failed", result.reason)

                # Single-turn mode (built-in tracker): one successful run == task done.
                if self.single_turn:
                    break

                try:
                    refreshed = self.linear.fetch_issue_states_by_ids([self.issue.id])
                except TrackerError as exc:
                    return self._finish(workspace, "failed", f"state refresh: {exc}")
                if refreshed:
                    self.issue = self._merge_state(refreshed[0])

                active = [s.lower() for s in self.config.active_states()]
                if self.issue.state_lower() not in active:
                    break
                if turn >= max_turns:
                    break
                turn += 1

            verdict = self._capture_verdict()
            if verdict is not None:
                verdict["used_tools"] = self._used_tools
            self._finish(workspace, "normal", None, verdict)
        except PromptError as exc:
            log.error("prompt error %s %s", ctx, exc)
            self._finish_no_workspace("failed", str(exc))
        except (WorkspaceError, RpcError) as exc:
            log.error("worker setup error %s %s", ctx, exc)
            self._finish_no_workspace("failed", str(exc))
        except Exception as exc:  # defensive: never leak a thread exception
            log.exception("worker crashed %s", ctx)
            self._finish_no_workspace("failed", str(exc))

    def _merge_state(self, refreshed: Issue) -> Issue:
        self.issue.state = refreshed.state
        if refreshed.labels:
            self.issue.labels = refreshed.labels
        return self.issue

    def _emit_turn_end(self, turn: int, reason: str) -> None:
        update = {"event": "turn_completed", "timestamp": _now(), "turn": turn, "reason": reason}
        if self._rpc:
            usage = self._rpc.get_token_usage()
            if usage:
                update["usage"] = usage
        self.emit_update(update)

    def _capture_verdict(self) -> Optional[dict]:
        """Parse the agent's final message for the structured finish protocol
        (STATUS / REOPEN / REASON). Returns None if nothing usable was found."""
        if not self._rpc:
            return None
        try:
            text = self._rpc.get_last_assistant_text()
        except Exception:
            return None
        return parse_verdict(text)

    def _finish(self, workspace, reason: str, error: Optional[str], verdict: Optional[dict] = None) -> None:
        self._finished.set()
        if self._rpc:
            self._rpc.stop()
        try:
            self.workspaces.run_hook("after_run", workspace.path, fatal=False)
        except Exception:
            pass
        self.emit_exit(reason, error, verdict)

    def _finish_no_workspace(self, reason: str, error: Optional[str]) -> None:
        self._finished.set()
        if self._rpc:
            self._rpc.stop()
        self.emit_exit(reason, error, None)
