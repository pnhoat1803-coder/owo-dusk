#!/usr/bin/env bash
# Starts the bot (uwu.py). Run it from any folder: bash start.sh [options for uwu.py]
# Run setup.sh first on a new PC.
set -u
cd "$(dirname "$0")" || { echo "[x] Cannot enter the project folder."; exit 1; }

PY=""
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
fi
if [ -z "$PY" ]; then
    echo "[x] Python was not found. Run setup.sh first."
    exit 1
fi

"$PY" uwu.py "$@"
exit $?
