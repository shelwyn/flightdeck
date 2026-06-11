from __future__ import annotations


class TrackerError(Exception):
    """Base error for any tracker backend (Linear or the built-in local tracker).

    The orchestrator catches this so backends are interchangeable.
    """

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
