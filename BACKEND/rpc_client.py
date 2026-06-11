from __future__ import annotations

import itertools
import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from logging_setup import get_logger, kv
from pi_models import pi_subprocess_env, parse_model_ref, build_pi_command

log = get_logger("orch.rpc")

_AFFIRMATIVE = ("allow", "yes", "approve", "accept", "proceed", "continue", "always")


class RpcError(Exception):
    pass


def _extract_pi_error_message(raw: Optional[str]) -> Optional[str]:
    """Pull a short human-readable message from Pi's errorMessage field."""
    if not raw:
        return None
    payload = raw
    if payload.startswith("Codex error: "):
        payload = payload[len("Codex error: ") :]
    try:
        obj = json.loads(payload)
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    except json.JSONDecodeError:
        pass
    return payload[:500] if len(payload) > 500 else payload


def _extract_message_text(message: dict) -> str:
    """Best-effort extraction of plain text from a pi assistant message object."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") in (None, "text") and block.get("text"):
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
    if parts:
        return "".join(parts)
    text = message.get("text")
    return str(text) if isinstance(text, str) else ""


class TurnResult:
    def __init__(self, ok: bool, reason: str, stop_reason: Optional[str] = None):
        self.ok = ok
        self.reason = reason
        self.stop_reason = stop_reason


class PiRpcClient:
    """Speaks Pi's `--mode rpc` JSONL protocol over stdio (app-server analog)."""

    def __init__(
        self,
        command: str,
        cwd: Path,
        session_id: str,
        session_name: str,
        read_timeout_ms: int,
        on_event: Callable[[dict], None],
        model_ref: Optional[str] = None,
    ):
        self.command = command
        self.cwd = cwd
        self.session_id = session_id
        self.session_name = session_name
        self.read_timeout = read_timeout_ms / 1000.0
        self.on_event = on_event
        self.model_ref = (model_ref or "").strip() or None

        self._proc: Optional[subprocess.Popen] = None
        self._stderr_lines: list[str] = []
        self._stdin_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        self._responses: dict[str, dict] = {}
        self._exited = threading.Event()

        self._turn_lock = threading.Lock()
        self._turn_done: Optional[threading.Event] = None
        self._turn_result: Optional[TurnResult] = None
        self._last_stop_reason: Optional[str] = None
        self._last_error_message: Optional[str] = None
        # Accumulated text of the in-flight assistant message (fallback for the
        # verdict parser when the get_last_assistant_text RPC is unavailable).
        self._cur_text_parts: list[str] = []
        self._last_assistant_text: Optional[str] = None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def start(self) -> None:
        spawn_cmd = build_pi_command(self.command, cwd=self.cwd)
        argv = shlex.split(spawn_cmd) + [
            "--session-id",
            self.session_id,
            "--name",
            self.session_name,
        ]
        env = pi_subprocess_env()
        # Invariant 1: the agent runs only in the per-issue workspace path.
        self._proc = subprocess.Popen(
            argv,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._wait_until_ready()
        self._apply_model_if_needed()

    def _stderr_tail(self) -> str:
        if not self._stderr_lines:
            return ""
        return self._stderr_lines[-1][:500]

    def _format_rpc_failure(self, detail: str) -> str:
        tail = self._stderr_tail()
        if tail and tail not in detail:
            return f"{detail} — {tail}"
        return detail

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + min(self.read_timeout, 45.0)
        last_err = "Pi RPC did not become ready"
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise RpcError(self._format_rpc_failure("Pi process exited during startup"))
            try:
                self.request("get_state", timeout=min(5.0, self.read_timeout))
                return
            except RpcError as exc:
                last_err = str(exc)
                time.sleep(0.15)
        raise RpcError(self._format_rpc_failure(last_err))

    def _apply_model_if_needed(self) -> None:
        if not self.model_ref:
            return
        provider, model_id = parse_model_ref(self.model_ref)
        if not provider or not model_id:
            raise RpcError(
                f"invalid model reference: {self.model_ref!r} (expected provider/model-id)"
            )
        resp = self.request(
            "set_model",
            provider=provider,
            modelId=model_id,
            timeout=min(60.0, self.read_timeout),
        )
        if not resp.get("success"):
            err = resp.get("error") or "set_model failed"
            raise RpcError(str(err))

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            line = raw.rstrip("\n")
            if line.endswith("\r"):
                line = line[:-1]
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-json rpc line: %s", line[:200])
                continue
            self._handle(obj)
        self._exited.set()
        self._fail_active_turn("port_exit")
        # Unblock any waiters on responses.
        with self._pending_lock:
            for event in self._pending.values():
                event["event"].set()

    def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in self._proc.stderr:
            text = raw.strip()
            if text:
                self._stderr_lines.append(text)
                log.warning("pi stderr: %s", text[:300])

    def _handle(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "response":
            self._resolve_response(obj)
            return
        if kind == "extension_ui_request":
            self._handle_ui_request(obj)
            return
        self._handle_event(obj)

    def _resolve_response(self, obj: dict) -> None:
        rid = obj.get("id")
        if rid is None:
            return
        with self._pending_lock:
            slot = self._pending.get(rid)
            if slot is not None:
                self._responses[rid] = obj
                slot["event"].set()

    def _handle_event(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "message_start":
            self._cur_text_parts = []
        elif kind == "message_update":
            ev = obj.get("assistantMessageEvent") or {}
            if ev.get("type") in ("text_delta", "text_start"):
                chunk = ev.get("text") or ev.get("delta") or ""
                if chunk:
                    self._cur_text_parts.append(chunk)
        elif kind == "message_end":
            message = obj.get("message") or {}
            role = message.get("role")
            stop = message.get("stopReason")
            if stop:
                self._last_stop_reason = stop
            err = message.get("errorMessage")
            if err and role == "assistant":
                self._last_error_message = str(err)
            text = _extract_message_text(message)
            if not text and self._cur_text_parts:
                text = "".join(self._cur_text_parts)
            if text and role == "assistant":
                self._last_assistant_text = text
        try:
            self.on_event(obj)
        except Exception as exc:  # observability must never crash the turn
            log.debug("on_event handler error: %s", exc)
        if kind == "agent_end":
            self._complete_turn()

    def _handle_ui_request(self, obj: dict) -> None:
        method = obj.get("method")
        rid = obj.get("id")
        if method == "select":
            options = obj.get("options") or []
            choice = next(
                (o for o in options if any(a in str(o).lower() for a in _AFFIRMATIVE)),
                options[0] if options else None,
            )
            self._send({"type": "extension_ui_response", "id": rid, "value": choice})
        elif method == "confirm":
            self._send({"type": "extension_ui_response", "id": rid, "confirmed": True})
        elif method in ("input", "editor"):
            # User-input-required: auto-resolve by dismissing so the run cannot stall.
            self._send({"type": "extension_ui_response", "id": rid, "cancelled": True})
        # notify/setStatus/setWidget/etc. are fire-and-forget; nothing to answer.

    def _send(self, obj: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise RpcError("rpc process not running")
        line = json.dumps(obj) + "\n"
        with self._stdin_lock:
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise RpcError(f"failed to write to rpc stdin: {exc}")

    def request(self, command_type: str, timeout: Optional[float] = None, **fields) -> dict:
        rid = str(next(self._ids))
        event = threading.Event()
        with self._pending_lock:
            self._pending[rid] = {"event": event}
        self._send({"id": rid, "type": command_type, **fields})
        if not event.wait(timeout if timeout is not None else self.read_timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise RpcError(f"timeout waiting for response to {command_type}")
        with self._pending_lock:
            self._pending.pop(rid, None)
            response = self._responses.pop(rid, None)
        if response is None:
            raise RpcError(
                self._format_rpc_failure(f"rpc stream closed before {command_type} response")
            )
        return response

    def get_session_id(self) -> Optional[str]:
        try:
            resp = self.request("get_state")
        except RpcError:
            return None
        return (resp.get("data") or {}).get("sessionId")

    def get_token_usage(self) -> dict:
        try:
            resp = self.request("get_session_stats")
        except RpcError:
            return {}
        return ((resp.get("data") or {}).get("tokens")) or {}

    def get_last_assistant_text(self) -> Optional[str]:
        """Return the text of the agent's most recent assistant message.

        Prefers the dedicated RPC command; falls back to text accumulated from
        the streaming events of the last completed message."""
        try:
            resp = self.request("get_last_assistant_text")
            if resp.get("success"):
                data = resp.get("data")
                if isinstance(data, str) and data.strip():
                    return data
                if isinstance(data, dict):
                    text = data.get("text") or _extract_message_text(data.get("message") or {})
                    if text:
                        return text
        except RpcError:
            pass
        return self._last_assistant_text

    def _complete_turn(self) -> None:
        with self._turn_lock:
            if self._turn_done is None or self._turn_result is not None:
                return
            stop = self._last_stop_reason
            if stop in ("error", "aborted"):
                detail = _extract_pi_error_message(self._last_error_message)
                reason = detail or f"turn_{stop}"
                snippet = (self._last_assistant_text or "")[:400]
                log.warning(
                    "pi turn failed %s",
                    kv(session=self.session_id, stop=stop, reason=reason, snippet=snippet or "(no assistant text)"),
                )
                self._turn_result = TurnResult(False, reason, stop)
            else:
                self._turn_result = TurnResult(True, "completed", stop)
            self._turn_done.set()

    def _fail_active_turn(self, reason: str) -> None:
        with self._turn_lock:
            if self._turn_done is not None and self._turn_result is None:
                self._turn_result = TurnResult(False, reason)
                self._turn_done.set()

    def run_turn(self, message: str, turn_timeout_ms: int) -> TurnResult:
        with self._turn_lock:
            self._turn_done = threading.Event()
            self._turn_result = None
            self._last_stop_reason = None
            self._last_error_message = None
            done = self._turn_done

        resp = self.request("prompt", message=message)
        if not resp.get("success"):
            err = resp.get("error")
            log.warning("pi prompt rejected %s", kv(session=self.session_id, error=err))
            with self._turn_lock:
                self._turn_done = None
            return TurnResult(False, "prompt_rejected: " + str(err))

        if not done.wait(turn_timeout_ms / 1000.0):
            log.warning("pi turn timeout %s", kv(session=self.session_id, timeout_ms=turn_timeout_ms))
            with self._turn_lock:
                self._turn_done = None
            self._send_abort()
            return TurnResult(False, "turn_timeout")

        with self._turn_lock:
            result = self._turn_result or TurnResult(False, "unknown")
            if not result.ok:
                log.warning(
                    "pi turn finished with error %s",
                    kv(session=self.session_id, reason=result.reason, stop=result.stop_reason),
                )
            self._turn_done = None
            self._turn_result = None
        return result

    def _send_abort(self) -> None:
        try:
            self._send({"type": "abort"})
        except RpcError:
            pass

    def stop(self) -> None:
        if not self._proc:
            return
        try:
            if self._proc.stdin:
                with self._stdin_lock:
                    self._proc.stdin.close()
        except Exception:
            pass
        self._terminate_process()

    def terminate(self) -> None:
        self._fail_active_turn("canceled")
        self._terminate_process()

    def _terminate_process(self) -> None:
        if not self._proc:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
