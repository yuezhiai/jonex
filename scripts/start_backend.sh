#!/usr/bin/env bash
# Jonex 平台 - CLI 后台启动后端服务(替代 VSCode Debug 的快捷方式)
# 用法: bash scripts/start_backend.sh [start|stop|status]
# 启动: Sidecar(8001) Gateway(8000) KB(8003) Business Domain(8005) Platform(8006)
set -e
cd "$(dirname "$0")/.."
LOG_DIR=logs/backend
mkdir -p "$LOG_DIR"
RUNTIME_FILE="$(pwd -W 2>/dev/null || pwd)/deploy/config/capability_runtime.yaml"

PY=./.venv/Scripts/python.exe
[ -x "$PY" ] || PY=./.venv/bin/python

SERVICES=(
  "sidecar:8001:main.py:"
  "gateway:8000:run_gateway.py:"
  "knowledge_base:8003:deploy/start_capability.py:CAPABILITY_KIND=business CAPABILITY_NAME=knowledge_base"
  "business_domain:8005:deploy/start_capability.py:CAPABILITY_KIND=business CAPABILITY_NAME=business_domain"
  "platform:8006:deploy/start_capability.py:CAPABILITY_KIND=business CAPABILITY_NAME=platform"
)

start_one() {
  local name=$1 port=$2 prog=$3 extra_env=$4
  if [ -f "$LOG_DIR/$name.pid" ] && kill -0 "$(cat "$LOG_DIR/$name.pid")" 2>/dev/null; then
    echo "$name($port) 已在运行 (pid $(cat "$LOG_DIR/$name.pid"))"
    return
  fi
  echo "启动 $name ($port) ..."
  (
    set -a; source .env.local; set +a
    export PYTHONUNBUFFERED=1 CAPABILITY_RUNTIME_FILE="$RUNTIME_FILE" SERVICE_PORT=$port SERVICE_ENDPOINT="http://127.0.0.1:$port" $extra_env
    exec "$PY" "$prog"
  ) > "$LOG_DIR/$name.log" 2>&1 &
  echo $! > "$LOG_DIR/$name.pid"
}

stop_all() {
  for name in sidecar gateway knowledge_base business_domain platform; do
    if [ -f "$LOG_DIR/$name.pid" ]; then
      kill "$(cat "$LOG_DIR/$name.pid")" 2>/dev/null && echo "停止 $name" || echo "$name 未运行"
      rm -f "$LOG_DIR/$name.pid"
    fi
  done
}

status_all() {
  for entry in "${SERVICES[@]}"; do
    IFS=: read -r name port _ _ <<<"$entry"
    if [ -f "$LOG_DIR/$name.pid" ] && kill -0 "$(cat "$LOG_DIR/$name.pid")" 2>/dev/null; then
      echo "$name: RUNNING (pid $(cat "$LOG_DIR/$name.pid"), port $port)"
    else
      echo "$name: STOPPED"
    fi
  done
}

case "${1:-start}" in
  start)
    for entry in "${SERVICES[@]}"; do
      IFS=: read -r name port prog extra <<<"$entry"
      start_one "$name" "$port" "$prog" "$extra"
    done
    echo "全部启动完成。日志: $LOG_DIR/*.log"
    ;;
  stop) stop_all ;;
  status) status_all ;;
  *) echo "用法: $0 [start|stop|status]"; exit 1 ;;
esac
