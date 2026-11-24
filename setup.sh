#!/usr/bin/env bash
# One-click setup for Linux, macOS and Termux. Safe to run again: it only adds or checks.
# Usage: bash setup.sh      (runs setup.py in this folder, from any current folder)
set -u
cd "$(dirname "$0")" || { echo "[x] Cannot enter the project folder."; exit 1; }

PY=""
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
fi
if [ -z "$PY" ]; then
    echo "[x] Python was not found. Install Python 3.12 or newer, then run this again."
    exit 1
fi

"$PY" setup.py "$@"
exit $?
