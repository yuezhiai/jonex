#!/bin/bash
# =============================================================================
# 数据库增量迁移执行器（版本表追踪 + 人工触发 + dry-run/确认）
#
# 背景：Docker PostgreSQL 的 /docker-entrypoint-initdb.d 只在数据卷首次初始化执行，
#   只覆盖 migrations/（全量 DDL）。后续迭代的表结构/字段变更统一沉淀为
#   update/NNN_*.sql（存量库增量 DDL，全部 IF [NOT] EXISTS 幂等）。
#
# 本脚本按文件名序应用 update/*.sql 到运行中的 jonex-postgres 容器。
# 迁移状态记录在 public.schema_migrations 版本表：已应用文件跳过、不重复执行，
# 执行成功才登记，失败中断不登记（修复后重跑将重试）。
# 迁移须由人工显式触发（make db-migrate），不再由 make up 自动执行。
#
# 用法：
#   bash deploy/postgres/update/apply.sh             # 交互确认后执行待应用迁移
#   bash deploy/postgres/update/apply.sh --dry-run   # 只预览待应用列表，不执行
#   bash deploy/postgres/update/apply.sh --yes       # 跳过确认直接执行（CI/自动化）
#   bash deploy/postgres/update/apply.sh --help      # 用法
#   make db-migrate / db-migrate-dry / db-migrate-yes
# 前提：jonex-postgres 容器运行中
#
# 环境变量覆盖：
#   JONEX_PG_CONTAINER  容器名（默认 jonex-postgres）
#   JONEX_PG_USER       用户（默认 jonex）
#   JONEX_PG_DB         数据库（默认 jonex）
# =============================================================================

set -euo pipefail

CONTAINER="${JONEX_PG_CONTAINER:-jonex-postgres}"
PG_USER="${JONEX_PG_USER:-jonex}"
PG_DB="${JONEX_PG_DB:-jonex}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- 参数解析 ----
DRY_RUN=0
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --help|-h)
      cat <<'EOF'
用法: apply.sh [--dry-run] [--yes]

  （无参数）交互确认后执行待应用迁移
  --dry-run  只预览待应用迁移列表，不执行、不登记
  --yes/-y   跳过确认直接执行（CI / 自动化）

环境变量: JONEX_PG_CONTAINER / JONEX_PG_USER / JONEX_PG_DB
EOF
      exit 0
      ;;
    *)
      echo "未知参数: $1（可用 --dry-run / --yes / --help）" >&2
      exit 2
      ;;
  esac
  shift
done

# 非交互环境（CI 或 stdin 非 TTY）自动视为 --yes，避免 read 卡死
if [[ $ASSUME_YES -eq 0 ]] && { [[ -n "${CI:-}" ]] || [[ ! -t 0 ]]; }; then
  ASSUME_YES=1
fi

# ---- psql 执行封装（-i 保持 stdin，-v ON_ERROR_STOP=1 遇错中断）----
psql_exec() {
  docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 "$@"
}

# ---- bootstrap 版本表 ----
psql_exec -q <<'SQL'
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename   VARCHAR(255) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

# ---- 读取已应用文件列表 ----
applied_file="$(mktemp)"
trap 'rm -f "$applied_file"' EXIT
docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tA \
  -c "SELECT filename FROM public.schema_migrations;" > "$applied_file"

# ---- 收集待应用 / 已应用 ----
shopt -s nullglob
pending=()
skipped=0
for f in "${SCRIPT_DIR}"/*.sql; do
  name="$(basename "$f")"
  if grep -qxF "$name" "$applied_file"; then
    echo "SKIP  ${name}（已应用）"
    skipped=$((skipped + 1))
  else
    pending+=("$f")
  fi
done

if [[ ${#pending[@]} -eq 0 ]]; then
  echo "==> 无待应用迁移（共 ${skipped} 个已应用）。"
  exit 0
fi

echo "==> 待应用迁移 ${#pending[@]} 个："
for f in "${pending[@]}"; do
  echo "    - $(basename "$f")"
done

if [[ $DRY_RUN -eq 1 ]]; then
  echo "==> [dry-run] 未执行任何迁移。"
  exit 0
fi

if [[ $ASSUME_YES -eq 0 ]]; then
  read -r -p "确认执行以上 ${#pending[@]} 个迁移？[y/N] " answer
  case "${answer:-}" in
    y|Y|yes|Yes|YES) : ;;
    *) echo "已取消，未执行任何迁移。"; exit 0 ;;
  esac
fi

# ---- 依次执行，成功才登记 ----
for f in "${pending[@]}"; do
  name="$(basename "$f")"
  echo "==> 应用 ${name} ..."
  if psql_exec < "$f"; then
    psql_exec -q -c "INSERT INTO public.schema_migrations (filename) VALUES ('${name}') ON CONFLICT (filename) DO NOTHING;"
    echo "    OK   ${name}"
  else
    echo "    FAIL ${name}（未登记，修复后重跑将重试此文件）" >&2
    exit 1
  fi
done

echo "==> 增量 DDL 应用完成：本次 ${#pending[@]} 个。"
