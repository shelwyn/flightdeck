from __future__ import annotations


class TrackerError(Exception):
    """Base error for the built-in SQLite tracker backend."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
