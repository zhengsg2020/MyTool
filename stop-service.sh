#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$ROOT/stop_service.py"
fi
if command -v python >/dev/null 2>&1; then
  exec python "$ROOT/stop_service.py"
fi
echo "[ERROR] python3/python not found in PATH"
exit 127
