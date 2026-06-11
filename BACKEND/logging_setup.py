from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_FLIGHTDECK_ROOT = Path(__file__).resolve().parent.parent
LOGS_ROOT = Path(__file__).resolve().parent.parent / "LOGS"
BACKEND_LOG_DIR = LOGS_ROOT / "backend"
FRONTEND_LOG_DIR = LOGS_ROOT / "frontend"

_LOCK = threading.Lock()
_CONFIGURED = False

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


class PrependFileHandler(logging.Handler):
    """Write log records at the top of the file (most recent entry first)."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            with self._lock:
                existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
                self.path.write_text(line + existing, encoding="utf-8")
        except Exception:
            self.handleError(record)


def _log_filename(component: str) -> Path:
    """One log file per day per component; started session also noted in the file."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (BACKEND_LOG_DIR if component == "backend" else FRONTEND_LOG_DIR) / f"{component}-{day}.log"


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FMT)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    root.addHandler(stderr)

    backend_file = PrependFileHandler(_log_filename("backend"))
    backend_file.setFormatter(formatter)
    root.addHandler(backend_file)

    started = datetime.now(timezone.utc).isoformat()
    root.info("logging configured backend_log=%s started_at=%s", backend_file.path, started)
    _CONFIGURED = True


def write_frontend_log(level: str, message: str, context: dict | None = None) -> None:
    """Append a client-side log line to LOGS/frontend (newest first)."""
    path = _log_filename("frontend")
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime(_DATE_FMT)
    level = (level or "info").upper()
    ctx = ""
    if context:
        parts = []
        for key, value in context.items():
            if value is None:
                continue
            text = str(value)
            if " " in text:
                text = text.replace('"', '\\"')
                text = f'"{text}"'
            parts.append(f"{key}={text}")
        if parts:
            ctx = " " + " ".join(parts)
    line = f"{ts} {level} frontend {message}{ctx}\n"
    with _LOCK:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(line + existing, encoding="utf-8")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def kv(**fields) -> str:
    """Render structured context as stable key=value pairs for log messages."""
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value)
        if " " in text:
            text = text.replace('"', '\\"')
            text = f'"{text}"'
        parts.append(f"{key}={text}")
    return " ".join(parts)
