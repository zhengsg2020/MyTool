#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./start-service.sh        默认后台运行（nohup）
#   ./start-service.sh --fg   前台运行

ROOT="$(cd "$(dirname "$0")" && pwd)"

LOG_FILE="$ROOT/service.log"
PID_FILE="$ROOT/service.pid"

if [[ "${1:-}" == "--fg" ]]; then
  shift
  exec python3 "$ROOT/serve.py" run "$@"
fi

# 默认后台运行：即使关闭终端也能继续，用 stop 脚本可停止服务
nohup python3 "$ROOT/serve.py" run "$@" >"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" > "$PID_FILE"
echo "Service started in background. PID=$pid, log=$LOG_FILE"
exit 0
