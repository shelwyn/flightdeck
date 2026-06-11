from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from pi_models import model_display


class ModelSettings:
    """Persisted Pi model choice for Flight Deck (global, not in WORKFLOW.md)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write({"configured": False, "model": None})

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"configured": False, "model": None}

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def is_configured(self) -> bool:
        with self._lock:
            return bool(self._read().get("configured"))

    def get_model(self) -> Optional[str]:
        with self._lock:
            raw = self._read().get("model")
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    def to_api(self) -> dict:
        with self._lock:
            data = self._read()
        model = data.get("model")
        model_ref = str(model).strip() if model else None
        if model_ref == "":
            model_ref = None
        return {
            "configured": bool(data.get("configured")),
            "model": model_ref,
            "label": model_display(model_ref),
        }

    def save(self, model: Optional[str]) -> dict:
        model_ref = (model or "").strip() or None
        with self._lock:
            self._write({"configured": True, "model": model_ref})
        return self.to_api()

    def logout(self) -> dict:
        with self._lock:
            self._write({"configured": False, "model": None})
        return self.to_api()
