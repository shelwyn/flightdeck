from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from config import Config
from logging_setup import get_logger, kv
from rpc_client import PiRpcClient, RpcError

log = get_logger("orch.llm")

# Cap auxiliary LLM calls (naming, commit messages) so they don't block for an hour.
_MAX_AUX_TURN_MS = 180_000
# Manual "Test Model" dialog may run longer (still bounded by workflow turn_timeout_ms).
_MAX_TEST_TURN_MS = 600_000


def pi_ask(
    config: Config,
    cwd: Path,
    session_id: str,
    session_name: str,
    prompt: str,
    *,
    pi_command: Optional[str] = None,
    model_ref: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> Optional[str]:
    """Run a single Pi RPC turn and return the assistant's plain-text reply.

    Uses the same `pi --mode rpc` command (and therefore the same configured LLM)
    as the planner and agent workers. Returns None on any failure."""
    cap = min(int(timeout_ms or config.turn_timeout_ms()), _MAX_AUX_TURN_MS)
    command = pi_command or config.codex_command()
    rpc: Optional[PiRpcClient] = None
    try:
        rpc = PiRpcClient(
            command=command,
            cwd=cwd,
            session_id=session_id,
            session_name=session_name,
            read_timeout_ms=config.read_timeout_ms(),
            on_event=on_event or (lambda _obj: None),
            model_ref=model_ref,
        )
        rpc.start()
        result = rpc.run_turn(prompt, cap)
        if not result.ok:
            log.warning("pi_ask turn failed (%s) session=%s", result.reason, session_id)
            return None
        text = rpc.get_last_assistant_text()
        return (text or "").strip() or None
    except (RpcError, Exception) as exc:
        log.warning("pi_ask error session=%s: %s", session_id, exc)
        return None
    finally:
        if rpc:
            try:
                rpc.stop()
            except Exception:
                pass


def query_pi_model(
    config: Config,
    prompt: str,
    cwd: Path,
    *,
    pi_command: Optional[str] = None,
    model_override: Optional[str] = None,
) -> dict:
    """One-shot prompt to the configured Pi model (Flight Deck "Test Model" dialog).

    Returns {"reply": str} on success or {"error": str} on failure."""
    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "prompt is required"}
    cap = min(int(config.turn_timeout_ms()), _MAX_TEST_TURN_MS)
    import uuid

    session_id = f"fd-test-{uuid.uuid4().hex[:12]}"
    command = pi_command or config.codex_command()
    rpc: Optional[PiRpcClient] = None
    try:
        rpc = PiRpcClient(
            command=command,
            cwd=cwd,
            session_id=session_id,
            session_name="Flight Deck · Test Model",
            read_timeout_ms=config.read_timeout_ms(),
            on_event=lambda _obj: None,
            model_ref=model_override,
        )
        rpc.start()
        result = rpc.run_turn(prompt, cap)
        if not result.ok:
            reason = result.reason or "turn failed"
            log.warning("model test failed %s", kv(reason=reason, session=session_id))
            return {"error": reason}
        text = (rpc.get_last_assistant_text() or "").strip()
        if not text:
            return {"error": "empty response from model"}
        log.info("model test ok %s", kv(session=session_id, chars=len(text)))
        return {"reply": text}
    except (RpcError, Exception) as exc:
        log.warning("model test error session=%s: %s", session_id, exc)
        return {"error": str(exc)}
    finally:
        if rpc:
            try:
                rpc.stop()
            except Exception:
                pass
