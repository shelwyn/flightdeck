from __future__ import annotations

from typing import Optional


def render_testing_prompt(
    *,
    project_slug: str,
    instructions: str,
    tasks: list[dict],
) -> str:
    """Build the adversarial QA agent prompt for an iteration sprint review."""
    lines = [
        "You are the **Quality Assurance agent** for this project — an adversarial reviewer.",
        "Your job is to verify that the build sprint actually delivered correct, working software.",
        "You are skeptical by default. Assume something is wrong until you have checked it yourself.",
        "",
        f"## Project: {project_slug}",
        "",
        "## Your testing instructions",
        "",
        (instructions or "").strip() or "(none provided)",
        "",
        "## Completed sprint tasks",
        "",
    ]
    if tasks:
        for t in tasks:
            ident = t.get("identifier") or "?"
            title = t.get("title") or ""
            desc = (t.get("description") or "").strip()
            lines.append(f"- **{ident}**: {title}")
            if desc:
                lines.append(f"  {desc}")
    else:
        lines.append("(no tasks listed)")
    lines.extend(
        [
            "",
            "## What to do",
            "",
            "- Inspect the shared project workspace on disk.",
            "- Run commands, read files, and execute checks aligned with the testing instructions above.",
            "- Look for missing deliverables, broken behavior, incomplete work, and regressions.",
            "- If a completed task is not actually done or is wrong, you MUST reopen it.",
            "",
            "## Finish protocol (required)",
            "",
            "End your FINAL message with a verdict block, each item on its own line:",
            "",
            "```",
            "STATUS: PASS    # sprint quality accepted",
            "REASON: <one short sentence>",
            "```",
            "",
            "Or, when issues remain:",
            "",
            "```",
            "STATUS: FAIL",
            "REOPEN: task-identifier-1, task-identifier-2",
            "REASON: <what is wrong and what must be fixed>",
            "```",
            "",
            "Use `REOPEN` with the exact task identifiers from the list above.",
            "Only report PASS when you are confident the sprint meets the testing instructions.",
            "**Do not kill processes or run port-cleanup against port 8787** — that is the Flight Deck dashboard.",
        ]
    )
    return "\n".join(lines)
