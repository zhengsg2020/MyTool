#!/usr/bin/env bash
# If ./deploy.sh fails with bash\r: sed -i 's/\r$//' "$0"  OR from repo root: make run
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -eq 0 ]]; then
  set -- run
fi
exec python3 "$ROOT/serve.py" "$@"
