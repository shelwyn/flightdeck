#!/usr/bin/env bash
# Flight Deck launcher: creates the venv + installs deps (first run), then starts the app.
# Usage: ./run.sh [WORKFLOW.md] [--port N]   (args are passed through to main.py)
set -euo pipefail

# Resolve this script's directory so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$SCRIPT_DIR/BACKEND"
VENV="$BACKEND/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

cd "$BACKEND"

# Pick a base interpreter for creating the venv.
PYBASE="$(command -v python3 || command -v python || true)"
if [ -z "$PYBASE" ]; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi

# Create the venv if missing.
if [ ! -x "$PY" ]; then
  echo "[flight-deck] creating virtualenv at BACKEND/.venv ..."
  "$PYBASE" -m venv "$VENV"
fi

# Install / sync dependencies. Use a stamp so we only reinstall when requirements change.
STAMP="$VENV/.requirements.installed"
if [ ! -f "$STAMP" ] || [ "$BACKEND/requirements.txt" -nt "$STAMP" ]; then
  echo "[flight-deck] installing requirements ..."
  "$PIP" install -q --upgrade pip >/dev/null 2>&1 || true
  "$PIP" install -q -r "$BACKEND/requirements.txt"
  touch "$STAMP"
fi

if ! command -v pi >/dev/null 2>&1; then
  echo "[flight-deck] warning: pi not found on PATH — install Pi before running agents" >&2
fi

echo "[flight-deck] starting app (open http://127.0.0.1:8787/) ..."
echo "[flight-deck] logs: $SCRIPT_DIR/LOGS/backend/ and $SCRIPT_DIR/LOGS/frontend/"
exec "$PY" main.py "$@"
