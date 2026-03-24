#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

if [[ $# -eq 0 ]]; then
  exec python3 "$ROOT/serve.py" run
fi

exec python3 "$ROOT/serve.py" "$@"
