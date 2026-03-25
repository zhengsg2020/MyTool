#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./start-service.sh        前台运行
#   ./start-service.sh --bg   后台运行（nohup），输出到 service.log，并写入 service.pid

ROOT="$(cd "$(dirname "$0")" && pwd)"

LOG_FILE="$ROOT/service.log"
PID_FILE="$ROOT/service.pid"

if [[ $# -eq 0 ]]; then
  exec python3 "$ROOT/serve.py" run
fi

if [[ "${1:-}" == "--bg" ]]; then
  shift
  # 后台运行：即使关闭终端也能继续，用端口脚本 Stop 直接停服务
  nohup python3 "$ROOT/serve.py" run "$@" >"$LOG_FILE" 2>&1 &
  pid=$!
  echo "$pid" > "$PID_FILE"
  echo "Service started in background. PID=$pid, log=$LOG_FILE"
  exit 0
fi

exec python3 "$ROOT/serve.py" "$@"
