from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from config import Config
from db import slugify
from llm_helper import pi_ask
from logging_setup import get_logger

log = get_logger("orch.naming")

_IDENT_SUFFIX_RE = re.compile(r"[^a-z0-9-]+")
_MAX_IDENT_LEN = 64
_MAX_COMMIT_LEN = 72


def _clean_suffix(raw: str) -> str:
    """Turn LLM output into a lowercase hyphenated suffix."""
    text = (raw or "").strip()
    # Take the first non-empty line; strip wrapping quotes/backticks.
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
    line = line.strip("\"'`")
    line = line.lower().replace(" ", "-")
    line = _IDENT_SUFFIX_RE.sub("-", line)
    line = re.sub(r"-+", "-", line).strip("-")
    return line[:40].strip("-") if line else ""


def _fallback_identifier(project_slug: str, seq: int) -> str:
    return f"{slugify(project_slug)}-{seq}"


def suggest_task_identifier(
    config: Config,
    cwd: Path,
    project_slug: str,
    title: str,
    description: Optional[str],
    existing: list[str],
    *,
    pi_command: Optional[str] = None,
    model_ref: Optional[str] = None,
) -> str:
    """Ask Pi's LLM for a readable task identifier derived from the title.

    Format: `{project_slug}-{meaningful-suffix}` (e.g. `football-reporter-build-api`).
    Falls back to `{project_slug}-{seq}` when the LLM is unavailable."""
    slug = slugify(project_slug)
    seq = len(existing) + 1
    fallback = _fallback_identifier(slug, seq)

    desc = (description or "").strip()
    if len(desc) > 400:
        desc = desc[:400] + "…"
    desc_block = f"\nDescription: {desc}" if desc else ""

    prompt = f"""You label tasks in a software project tracker. Given the task below, reply with ONLY a short identifier suffix: 2-5 lowercase English words joined by hyphens. No spaces, no punctuation, no explanation.

Good examples: build-backend-api, test-login-flow, add-user-dashboard, fix-payment-bug
Bad examples: task-1, FOOTBALL-2, "Build API"

Project slug: {slug}
Task title: {title.strip()}{desc_block}

Reply with ONLY the suffix (e.g. build-backend-api):"""

    text = pi_ask(
        config,
        cwd,
        session_id=f"name-task-{slug}",
        session_name=f"Flight Deck naming · {slug}",
        prompt=prompt,
        pi_command=pi_command,
        model_ref=model_ref,
    )
    suffix = _clean_suffix(text or "")
    if not suffix:
        log.info("task naming fallback for %r -> %s", title, fallback)
        return _unique(fallback, existing, slug, seq)

    candidate = f"{slug}-{suffix}"[:_MAX_IDENT_LEN].strip("-")
    ident = _unique(candidate, existing, slug, seq)
    log.info("task named %r -> %s", title, ident)
    return ident


def _unique(base: str, existing: list[str], slug: str, seq: int) -> str:
    taken = {i.lower() for i in existing}
    if base.lower() not in taken:
        return base
    for n in range(2, 100):
        candidate = f"{base}-{n}"[:_MAX_IDENT_LEN]
        if candidate.lower() not in taken:
            return candidate
    return _fallback_identifier(slug, seq)


def suggest_commit_message(
    config: Config,
    cwd: Path,
    project_slug: str,
    project_title: str,
    completed_tasks: list[dict],
    changed_files: list[str],
    *,
    pi_command: Optional[str] = None,
    model_ref: Optional[str] = None,
) -> Optional[str]:
    """Ask Pi's LLM for a readable one-line git commit subject."""
    if not completed_tasks:
        return None

    task_lines = []
    for t in completed_tasks:
        role = t.get("role") or "build"
        task_lines.append(f"- {t.get('identifier', '?')}: {t.get('title', '?')} ({role})")
    tasks_block = "\n".join(task_lines)

    files_block = ""
    if changed_files:
        shown = changed_files[:30]
        files_block = "\nChanged files:\n" + ", ".join(shown)
        if len(changed_files) > 30:
            files_block += f" … (+{len(changed_files) - 30} more)"

    prompt = f"""Write ONE git commit message subject line summarizing the sprint work below.
Use clear, plain English in imperative mood (e.g. "Add user login API and tests").
Prefer conventional prefixes when they fit: feat:, fix:, test:, chore:
Maximum {_MAX_COMMIT_LEN} characters. No quotes, no markdown, no body — subject only.

Project: {project_title or project_slug}
Completed tasks:
{tasks_block}{files_block}

Reply with ONLY the commit message subject line:"""

    text = pi_ask(
        config,
        cwd,
        session_id=f"commit-{project_slug}",
        session_name=f"Flight Deck commit · {project_slug}",
        prompt=prompt,
        pi_command=pi_command,
        model_ref=model_ref,
    )
    if not text:
        return None
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), text.strip())
    line = line.strip("\"'`")
    # Drop accidental "Subject:" prefixes.
    if line.lower().startswith("subject:"):
        line = line.split(":", 1)[1].strip()
    if len(line) > _MAX_COMMIT_LEN:
        line = line[: _MAX_COMMIT_LEN - 1].rstrip() + "…"
    return line or None


def list_staged_files(repo: Path) -> list[str]:
    """Return paths staged for commit (after `git add -A`)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []
