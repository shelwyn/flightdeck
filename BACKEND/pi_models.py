from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Optional

from logging_setup import get_logger

log = get_logger("orch.models")

_LIST_TIMEOUT_S = 90
_HEADER_RE = re.compile(r"^\s*provider\s+model\s+", re.IGNORECASE)
_ROW_RE = re.compile(r"^(\S+)\s+(\S+)\s+")
_LITELLM_CACHE = "litellm-models.json"

_login_env_cache: Optional[dict[str, str]] = None
_login_env_lock = threading.Lock()


def _agent_dir() -> Path:
    raw = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".pi" / "agent"


def _read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _package_sources(settings: dict) -> list[str]:
    out: list[str] = []
    for entry in settings.get("packages") or []:
        if isinstance(entry, str):
            src = entry.strip()
        elif isinstance(entry, dict):
            src = str(entry.get("source") or "").strip()
        else:
            continue
        if src:
            out.append(src)
    return out


def pi_configured_packages(cwd: Optional[Path] = None) -> list[str]:
    """Pi extension packages from Flight Deck, project, and global settings."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(sources: list[str]) -> None:
        for src in sources:
            if src in seen:
                continue
            seen.add(src)
            ordered.append(src)

    # Flight Deck install dir (optional .pi/settings.json from run.sh or manual setup).
    flight_deck_root = Path(__file__).resolve().parent.parent
    add(_package_sources(_read_settings(flight_deck_root / ".pi" / "settings.json")))

    if cwd is not None:
        add(_package_sources(_read_settings(Path(cwd) / ".pi" / "settings.json")))

    agent_dir = _agent_dir()
    add(_package_sources(_read_settings(agent_dir / "settings.json")))
    # Legacy install location (some setups still have packages here).
    add(_package_sources(_read_settings(Path.home() / ".pi" / "settings.json")))
    return ordered


def _litellm_base_url_from_pi_cache() -> Optional[str]:
    path = _agent_dir() / _LITELLM_CACHE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        base = str(data.get("baseUrl") or "").strip()
        return base or None
    except Exception:
        return None


def _parse_env0(data: bytes) -> dict[str, str]:
    env: dict[str, str] = {}
    for part in data.split(b"\0"):
        if not part or b"=" not in part:
            continue
        key, _, val = part.partition(b"=")
        try:
            env[key.decode()] = val.decode()
        except UnicodeDecodeError:
            continue
    return env


def login_shell_env() -> dict[str, str]:
    """Capture environment from a login shell (same surface Pi sees in a terminal)."""
    global _login_env_cache
    with _login_env_lock:
        if _login_env_cache is not None:
            return _login_env_cache
        try:
            proc = subprocess.run(
                ["bash", "-lc", "env -0"],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 0 and proc.stdout:
                _login_env_cache = _parse_env0(proc.stdout)
            else:
                _login_env_cache = {}
        except Exception as exc:
            log.warning("could not read login shell env: %s", exc)
            _login_env_cache = {}
        return _login_env_cache


def pi_subprocess_env() -> dict[str, str]:
    """Environment for Pi child processes — Pi manages credentials; inherit login shell."""
    merged = dict(login_shell_env())
    merged.update(os.environ)
    if not (merged.get("LITELLM_BASE_URL") or "").strip():
        base = _litellm_base_url_from_pi_cache()
        if base:
            merged["LITELLM_BASE_URL"] = base
    return merged


def _model_entry(provider: str, model_id: str) -> dict:
    ref = f"{provider}/{model_id}"
    return {
        "provider": provider,
        "model": model_id,
        "ref": ref,
        "label": model_display(ref),
    }


def build_pi_command(
    base_command: str,
    model_ref: Optional[str] = None,
    *,
    cwd: Optional[Path] = None,
) -> str:
    """Return the Pi spawn command (base only). Model is applied via RPC ``set_model``."""
    del model_ref
    parts = shlex.split((base_command or "").strip() or "pi --mode rpc")
    cleaned: list[str] = []
    skip = False
    for part in parts:
        if skip:
            skip = False
            continue
        if part in ("--model", "-e", "--extension"):
            skip = True
            continue
        cleaned.append(part)
    for pkg in pi_configured_packages(cwd):
        cleaned.extend(["-e", pkg])
    return " ".join(shlex.quote(p) for p in cleaned)


def parse_model_ref(model_ref: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split ``provider/model-id`` on the first slash (model ids may contain slashes)."""
    raw = (model_ref or "").strip()
    if not raw:
        return None, None
    if "/" not in raw:
        return None, raw
    provider, model_id = raw.split("/", 1)
    provider = provider.strip()
    model_id = model_id.strip()
    if not provider or not model_id:
        return None, None
    return provider, model_id


def model_display(model_ref: Optional[str]) -> str:
    if not (model_ref or "").strip():
        return "Pi default"
    ref = model_ref.strip()
    if "/" in ref:
        provider, model_id = ref.split("/", 1)
        return f"{provider} / {model_id}"
    return ref


def _models_json_paths(cwd: Optional[Path] = None) -> list[Path]:
    paths: list[Path] = []
    if cwd is not None:
        paths.append(Path(cwd) / ".pi" / "agent" / "models.json")
    agent_dir = _agent_dir()
    paths.append(agent_dir / "models.json")
    return paths


def _pi_settings_paths(cwd: Optional[Path] = None) -> list[Path]:
    paths: list[Path] = []
    if cwd is not None:
        paths.append(Path(cwd) / ".pi" / "settings.json")
    paths.append(_agent_dir() / "settings.json")
    paths.append(Path.home() / ".pi" / "settings.json")
    return paths


def pi_default_model_ref(cwd: Optional[Path] = None) -> Optional[str]:
    """Pi default provider/model from settings (more specific paths override global)."""
    provider = ""
    model_id = ""
    for path in reversed(_pi_settings_paths(cwd)):
        settings = _read_settings(path)
        p = str(settings.get("defaultProvider") or "").strip()
        m = str(settings.get("defaultModel") or "").strip()
        if p:
            provider = p
        if m:
            model_id = m
    if provider and model_id:
        return f"{provider}/{model_id}"
    return None


def _models_from_models_json(cwd: Optional[Path] = None) -> list[dict]:
    """Explicit models declared in Pi ``models.json`` (not provider discovery catalogs)."""
    models: list[dict] = []
    seen: set[str] = set()
    for path in _models_json_paths(cwd):
        data = _read_settings(path)
        providers = data.get("providers")
        if not isinstance(providers, dict):
            continue
        for provider_name, provider_cfg in providers.items():
            if not isinstance(provider_cfg, dict):
                continue
            provider = str(provider_name).strip()
            if not provider:
                continue
            for model_def in provider_cfg.get("models") or []:
                if isinstance(model_def, str):
                    model_id = model_def.strip()
                    display_name = None
                elif isinstance(model_def, dict):
                    model_id = str(model_def.get("id") or "").strip()
                    display_name = str(model_def.get("name") or "").strip() or None
                else:
                    continue
                if not model_id:
                    continue
                ref = f"{provider}/{model_id}"
                if ref in seen:
                    continue
                seen.add(ref)
                entry = _model_entry(provider, model_id)
                if display_name:
                    entry["label"] = f"{provider} / {display_name}"
                models.append(entry)
    return models


def list_pi_configured_models(cwd: Optional[Path] = None) -> dict:
    """Models from Pi default settings and ``models.json`` — not full discovery catalogs."""
    models: list[dict] = []
    seen: set[str] = set()

    default_ref = pi_default_model_ref(cwd)
    if default_ref:
        provider, model_id = parse_model_ref(default_ref)
        if provider and model_id:
            entry = _model_entry(provider, model_id)
            entry["is_default"] = True
            models.append(entry)
            seen.add(entry["ref"])

    for entry in _models_from_models_json(cwd):
        if entry["ref"] in seen:
            continue
        seen.add(entry["ref"])
        models.append(entry)

    if not models:
        return {
            "models": [],
            "scope": "configured",
            "default_ref": default_ref,
            "error": (
                "No Pi default or models.json entries found. "
                "Set defaultProvider/defaultModel in Pi, or add models to ~/.pi/agent/models.json."
            ),
        }
    return {"models": models, "scope": "configured", "default_ref": default_ref, "error": None}


def list_pi_models(cwd: Optional[Path] = None) -> dict:
    """Run ``pi --list-models`` with Pi's login-shell credential surface."""
    env = pi_subprocess_env()
    cmd = build_pi_command("pi --list-models", cwd=cwd)
    try:
        proc = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=_LIST_TIMEOUT_S,
            env=env,
        )
    except FileNotFoundError:
        return {"models": [], "error": "pi command not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"models": [], "error": "pi --list-models timed out"}

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = [ln.rstrip() for ln in combined.splitlines() if ln.strip()]
    if not lines:
        return {"models": [], "error": "pi --list-models produced no output"}

    if any("No models available" in ln for ln in lines):
        hint = next((ln for ln in lines if "No models available" in ln), "No models available")
        return {
            "models": [],
            "error": (
                f"{hint.strip()} Configure providers in Pi (/login, env vars), then Refresh."
            ),
        }

    models: list[dict] = []
    in_table = False
    for line in lines:
        if _HEADER_RE.match(line):
            in_table = True
            continue
        if not in_table:
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        provider, model_id = m.group(1), m.group(2)
        models.append(_model_entry(provider, model_id))

    if not models:
        tail = lines[-1] if lines else "unknown error"
        return {"models": [], "error": tail}
    return {"models": models, "scope": "all", "error": None}
