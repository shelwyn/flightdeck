"""Seed the built-in tracker with a demo project and a couple of Todo tasks.

Usage:
    python seed.py
"""
from __future__ import annotations

import os
from pathlib import Path

from db import Db, DbError


def main() -> int:
    default_db = Path(__file__).resolve().parent.parent / "DATA" / "tracker.db"
    db = Db(Path(os.environ.get("FLIGHTDECK_DB", default_db)))
    slug = "demo"
    try:
        db.create_project(
            title="Demo Project",
            description="Sample project created by seed.py",
            slug=slug,
        )
        print(f"created project: {slug}")
    except DbError as exc:
        print(f"project: {exc}")

    samples = [
        ("Create a hello.txt file", "Write a file named hello.txt containing 'hello from the orchestrator'."),
        ("Add a README", "Create a short README.md describing this demo workspace."),
    ]
    iters = db.list_iterations(slug)
    if not iters:
        print("no iteration for demo project")
        return 1
    iteration_id = iters[0]["id"]
    for title, desc in samples:
        task = db.create_task(slug, title, desc, priority=2, iteration_id=iteration_id)
        print(f"created task: {task['identifier']} - {task['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
