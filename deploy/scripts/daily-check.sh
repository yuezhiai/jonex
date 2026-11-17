#!/usr/bin/env bash
# =============================================================================
# 生产日常巡检脚本（只读：不改配置、不动数据、不重建容器）
#
# 用法（在交付包目录 / 仓库 deploy 目录都可，脚本自己定位 compose 目录）:
#   bash scripts/daily-check.sh                 # 交付包里的位置
#   bash deploy/scripts/daily-check.sh          # 仓库里的位置
#
# 检查项: 端口暴露面 / 容器状态 / 非 healthy / HTTP 入口 / 资源磁盘 / 数据库 / 待应用迁移
# 手册: docs/ops/production-operations-manual.md §3
#
# 环境变量（可选）:
#   COMPOSE_FILE   compose 文件集；默认 docker-compose.yml（= 生产档，只发 80/443/39301）
#                  本机开发调试时才需要叠加覆盖（生产不要这样做）：
#   export COMPOSE_FILE="docker-compose.yml:docker-compose.mac.yml:docker-compose.override.yml"
#
# 退出码: 0 = 巡检跑完（不代表全绿，逐段看输出）; 1 = 找不到 compose 目录
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$DEPLOY_DIR" || { echo "找不到 compose 目录: $DEPLOY_DIR" >&2; exit 1; }

export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

# 生产允许对外发布的容器（只有这两个）
ALLOW_EXPOSED="jonex-frontend-gateway"

echo "== compose 文件集 =="
echo "  $COMPOSE_FILE"

echo; echo "== 端口暴露面（生产只应 jonex-frontend-gateway 80/443）=="
exposed_bad=0
while IFS='|' read -r name pubs; do
  # .Publishers 形如 [{0.0.0.0 80 80 tcp} {:: 80 80 tcp}]；expose-only 的 URL 为空、PublishedPort 为 0
  pub=$(printf '%s' "$pubs" | grep -o '{0\.0\.0\.0 [0-9]* [0-9]* tcp}' | awk '{print $3"->"$2}' | paste -sd, -)
  [ -z "$pub" ] && continue
  allowed=0
  for ok in $ALLOW_EXPOSED; do [ "$name" = "$ok" ] && allowed=1; done
  if [ "$allowed" = "1" ]; then
    echo "  ok   ${name} → ${pub}"
  else
    echo "  ⚠ 意外暴露 ${name} → ${pub}（生产不应出现，见手册 §1.3 / §7.2）"
    exposed_bad=1
  fi
done < <(docker compose ps --format '{{.Name}}|{{.Publishers}}')
[ "$exposed_bad" = "1" ] || echo "  （暴露面正常）"

echo; echo "== 容器状态 =="
docker compose ps --format 'table {{.Name}}\t{{.Status}}'

echo; echo "== 非 healthy 的服务 =="
found=0
for c in $(docker ps --format '{{.Names}}'); do
  s=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$c")
  if [ "$s" != "healthy" ]; then
    echo "  $c → $s"
    found=1
  fi
done
[ "$found" = "1" ] || echo "  （全部 healthy）"

echo; echo "== HTTP 入口 =="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/health)
echo "  http  /health → $code $([ "$code" = "200" ] || echo '⚠')"
mode=$(grep -E '^ACCESS_MODE=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')
if [ "${mode:-http}" = "https" ]; then
  hcode=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://127.0.0.1/health)
  echo "  https /health → ${hcode}（ACCESS_MODE=https，必须 200）"
else
  echo "  （ACCESS_MODE=${mode:-http}，跳过 https 检查）"
fi

echo; echo "== 资源 / 磁盘 =="
docker system df | tail -4
df -h / | tail -1
du -sh data 2>/dev/null | tail -1

echo; echo "== 数据库 =="
docker exec jonex-postgres pg_isready -U jonex -d jonex
docker exec jonex-postgres psql -U jonex -d jonex -tAc \
  "SELECT 'applied_migrations=' || count(*) FROM public.schema_migrations;"
docker exec jonex-postgres psql -U jonex -d jonex -tAc \
  "SELECT 'db_size=' || pg_size_pretty(pg_database_size('jonex'));"
docker exec jonex-postgres psql -U jonex -d jonex -tAc \
  "SELECT 'long_tx_gt_5min=' || count(*) FROM pg_stat_activity
   WHERE xact_start IS NOT NULL AND now()-xact_start > interval '5 min';"

echo; echo "== 备份目录 =="
if [ -d data/backup ]; then
  ls -lt data/backup 2>/dev/null | head -4
else
  echo "  （无 data/backup：确认是否已配备份 cron，见手册 §5.3）"
fi

echo; echo "== 待应用迁移（只预览）=="
bash postgres/update/apply.sh --dry-run 2>/dev/null | tail -4

echo; echo "== 完成 =="
