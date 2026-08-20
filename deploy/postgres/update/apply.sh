#!/bin/bash
# =============================================================================
# 数据库增量迁移执行器（幂等，可重复执行）
#
# 背景：Docker PostgreSQL 的 /docker-entrypoint-initdb.d 只在数据卷首次初始化执行，
#   只覆盖 migrations/（全量 DDL）。后续迭代的表结构/字段变更统一沉淀为
#   update/NNN_*.sql（存量库增量 DDL，全部 IF [NOT] EXISTS 幂等）。
#
# 本脚本按文件名序依次应用 update/*.sql 到运行中的 jonex-postgres 容器。
# 新老数据库都能安全执行：全新库已由 migrations/ 建齐，重复执行 update/ 无副作用。
#
# 用法：
#   bash deploy/postgres/update/apply.sh
#   make db-migrate                          # Makefile 快捷方式
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

echo "==> 应用增量 DDL 到容器 ${CONTAINER} (${PG_USER}@${PG_DB}) ..."

shopt -s nullglob
applied=0
for f in "${SCRIPT_DIR}"/*.sql; do
  name="$(basename "$f")"
  echo "==> [${applied}] ${name}"
  docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 < "$f"
  applied=$((applied + 1))
done

if [[ $applied -eq 0 ]]; then
  echo "!! 未找到 update/*.sql，跳过。"
else
  echo "==> 增量 DDL 应用完成：共 ${applied} 个文件。"
fi
