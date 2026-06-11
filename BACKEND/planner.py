from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from config import Config
from logging_setup import get_logger, kv
from models import Issue
from rpc_client import PiRpcClient, RpcError

log = get_logger("orch.planner")

_PLAN_PROMPT = """You are the planning lead for a software project. Below are the
tasks currently queued in the project's Todo column. Decide the order in which an
autonomous agent should execute them, which subfolder of the shared project
workspace each task should live in, and the dependencies between tasks.

Rules:
- "depends_on" lists the identifiers of tasks that MUST finish before this task starts.
- "subdir" is a short, lowercase, hyphen/slash-free folder name (e.g. "backend", "frontend", "tests").
- Use the exact task identifiers given below.

Tasks:
{tasks}

Reply with ONLY a single JSON object, no prose, in this exact shape:
{{"tasks": [{{"identifier": "ABC-1", "subdir": "backend", "depends_on": []}}]}}
"""


def _format_tasks(issues: list[Issue]) -> str:
    lines = []
    for i in issues:
        desc = (i.description or "").strip().replace("\n", " ")
        if len(desc) > 280:
            desc = desc[:280] + "…"
        lines.append(f"- {i.identifier}: {i.title}\n    {desc}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first balanced {...} JSON object out of an LLM reply."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for idx in range(start, len(text)):
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : idx + 1]
                    break
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _fallback_plan(issues: list[Issue]) -> list[dict]:
    """Creation-order, no dependencies — used when the LLM is unavailable."""
    return [{"identifier": i.identifier, "subdir": None, "depends_on": []} for i in issues]


def _normalize(plan_obj: dict, issues: list[Issue]) -> list[dict]:
    valid_ids = {i.identifier for i in issues}
    raw = plan_obj.get("tasks") if isinstance(plan_obj, dict) else None
    if not isinstance(raw, list):
        return _fallback_plan(issues)
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("identifier") or "").strip()
        if ident not in valid_ids or ident in seen:
            continue
        seen.add(ident)
        subdir = item.get("subdir")
        subdir = str(subdir).strip() if subdir else None
        if subdir:
            subdir = re.sub(r"[^A-Za-z0-9._/-]", "-", subdir).strip("/") or None
        depends = [str(d).strip() for d in (item.get("depends_on") or []) if str(d).strip() in valid_ids]
        out.append({"identifier": ident, "subdir": subdir, "depends_on": depends})
    for i in issues:
        if i.identifier not in seen:
            out.append({"identifier": i.identifier, "subdir": None, "depends_on": []})
    return out


def plan_tasks(
    config: Config,
    project_slug: str,
    cwd: Path,
    issues: list[Issue],
    *,
    pi_command: Optional[str] = None,
    model_ref: Optional[str] = None,
) -> list[dict]:
    """Ask the LLM to sequence the given Todo tasks. Always returns a usable plan."""
    if not issues:
        return []
    prompt = _PLAN_PROMPT.format(tasks=_format_tasks(issues))
    rpc: Optional[PiRpcClient] = None
    try:
        rpc = PiRpcClient(
            command=pi_command or config.codex_command(),
            cwd=cwd,
            session_id=f"planner-{project_slug}",
            session_name=f"Flight Deck planner · {project_slug}",
            read_timeout_ms=config.read_timeout_ms(),
            on_event=lambda obj: None,
            model_ref=model_ref,
        )
        rpc.start()
        result = rpc.run_turn(prompt, config.turn_timeout_ms())
        if not result.ok:
            log.warning("planner turn failed (%s); using creation-order fallback", result.reason)
            return _fallback_plan(issues)
        text = rpc.get_last_assistant_text()
        plan_obj = _extract_json(text or "")
        if not plan_obj:
            log.warning("planner produced no parseable JSON; using creation-order fallback")
            return _fallback_plan(issues)
        plan = _normalize(plan_obj, issues)
        log.info("planner produced plan %s", kv(project=project_slug, tasks=len(plan)))
        return plan
    except (RpcError, Exception) as exc:
        log.warning("planner error (%s); using creation-order fallback", exc)
        return _fallback_plan(issues)
    finally:
        if rpc:
            try:
                rpc.stop()
            except Exception:
                pass
