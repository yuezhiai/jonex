#!/usr/bin/env bash
# ============================================================================
# 校验 `deploy/postgres/migrations/` 建出的 schema 与「目标库」完全一致。
#
# 为什么需要它：migrations/ 是「按生产库终态重新生成」的建库脚本（2026-11-12 改造）。
# 生成过程一旦漏东西，只有真建一个库才能发现 —— 靠读文件、靠跑服务都不够（历史上就被
# 「先建表 + 后续 ALTER」的补丁块坑过三轮）。本脚本把「新库 vs 目标库」的差异机器化：
#   * 列（名称 + 类型 + 可空 + 默认值）
#   * 约束（名称 + 定义）
#   * 索引（名称 + 定义）
#   * 视图（名称 + 定义）
#
# 用法：
#   bash deploy/postgres/verify_schema_parity.sh                  # 与本机 jonex 库比
#   bash deploy/postgres/verify_schema_parity.sh --target jonex   # 指定目标库
#   bash deploy/postgres/verify_schema_parity.sh --keep           # 保留临时库便于排查
#
# 环境变量：JONEX_PG_CONTAINER（默认 jonex-postgres）、PG 用户/库名走 --target。
# 退出码：0 = 一致；1 = 有差异（差异明细打到 stdout，末尾给计数）。
# ============================================================================
set -euo pipefail

CONTAINER="${JONEX_PG_CONTAINER:-jonex-postgres}"
PG_USER="${JONEX_PG_USER:-jonex}"
TARGET_DB="jonex"
SCRATCH_DB="jonex_parity_scratch"
KEEP=0
MIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/migrations"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET_DB="$2"; shift 2 ;;
    --scratch) SCRATCH_DB="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

psql_target() { docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$TARGET_DB" -tA "$@"; }
psql_scratch() { docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$SCRATCH_DB" -tA "$@"; }
psql_admin() { docker exec -i "$CONTAINER" psql -U "$PG_USER" -d postgres -tA "$@"; }

# 只比「我们要建」的 schema；排除升级过程留下的备份表与外部引擎自建的对象
SCHEMAS="('platform','knowledge_base','business_domain','task_center','metering','monitor','data_governance','ontology_compile')"
EXCLUDE_TABLE_REGEX="(_bak_[0-9]+$|mcp_keys_pre_|mcp_write_keys_archive|_pre_[a-z_]+$)"

cat > /tmp/_parity_queries.sql <<SQL
\pset footer off
-- 列
SELECT c.table_schema||'.'||c.table_name||'.'||c.column_name||' :: '||
       c.data_type||' null='||c.is_nullable||' default='||coalesce(c.column_default,'-')
FROM information_schema.columns c
WHERE c.table_schema IN $SCHEMAS
  AND (c.table_schema||'.'||c.table_name) !~ '$EXCLUDE_TABLE_REGEX'
ORDER BY 1;
-- 约束
SELECT n.nspname||'.'||c.relname||' | '||co.conname||' :: '||pg_get_constraintdef(co.oid)
FROM pg_constraint co
JOIN pg_class c ON c.oid = co.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN $SCHEMAS
  AND (n.nspname||'.'||c.relname) !~ '$EXCLUDE_TABLE_REGEX'
ORDER BY 1;
-- 索引
SELECT n.nspname||'.'||c.relname||' | '||i.relname||' :: '||pg_get_indexdef(x.indexrelid)
FROM pg_index x
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class i ON i.oid = x.indexrelid
WHERE n.nspname IN $SCHEMAS
  AND (n.nspname||'.'||c.relname) !~ '$EXCLUDE_TABLE_REGEX'
ORDER BY 1;
-- 视图
SELECT n.nspname||'.'||c.relname||' :: '||md5(pg_get_viewdef(c.oid, true))
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','m') AND n.nspname IN $SCHEMAS
ORDER BY 1;
SQL

echo "==> 建临时库 $SCRATCH_DB 并执行 migrations/"
psql_admin -q -c "DROP DATABASE IF EXISTS $SCRATCH_DB" >/dev/null
psql_admin -q -c "CREATE DATABASE $SCRATCH_DB" >/dev/null

for f in "$MIG_DIR"/*.sql; do
  if ! docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$SCRATCH_DB" -v ON_ERROR_STOP=1 -q < "$f" >/dev/null 2>/tmp/_parity_err.txt; then
    echo "❌ 执行失败：$(basename "$f")"
    head -5 /tmp/_parity_err.txt
    [[ $KEEP -eq 0 ]] && psql_admin -q -c "DROP DATABASE IF EXISTS $SCRATCH_DB" >/dev/null
    exit 1
  fi
done

echo "==> 与目标库 $TARGET_DB 逐项比对"
docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$SCRATCH_DB" -tA < /tmp/_parity_queries.sql > /tmp/_parity_new.txt
docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$TARGET_DB"  -tA < /tmp/_parity_queries.sql > /tmp/_parity_ref.txt

# pg_get_constraintdef / pg_get_indexdef 对同一约束的渲染可能因「写法来源」不同而不同
# （例如 CHECK 里的 ANY(ARRAY[...]) 与手写的 IN (...)，索引 WHERE 的括号层级），
# 这些是等价写法。比对时抹掉 cast、括号、空白，只留标识符与字面量。
norm() {
  sed -E 's/::(character varying|text|bpchar|character|name)(\[\])?//g' "$1" \
    | tr -d '()' \
    | sed -E 's/[[:space:]]+/ /g; s/ +$//' \
    | LC_ALL=C sort
}
norm /tmp/_parity_new.txt > /tmp/_parity_new_norm.txt
norm /tmp/_parity_ref.txt > /tmp/_parity_ref_norm.txt

NEW_N=$(wc -l < /tmp/_parity_new_norm.txt | tr -d ' ')
REF_N=$(wc -l < /tmp/_parity_ref_norm.txt | tr -d ' ')
ONLY_NEW=$(LC_ALL=C comm -23 /tmp/_parity_new_norm.txt /tmp/_parity_ref_norm.txt)
ONLY_REF=$(LC_ALL=C comm -13 /tmp/_parity_new_norm.txt /tmp/_parity_ref_norm.txt)
DIFF_N=$(printf '%s\n%s\n' "$ONLY_NEW" "$ONLY_REF" | grep -c '[^[:space:]]' || true)

echo "    新库 $NEW_N 条 / 目标库 $REF_N 条"
if [[ "$DIFF_N" -eq 0 ]]; then
  echo "✅ 一致（列/约束/索引/视图 0 差异）"
  [[ $KEEP -eq 0 ]] && psql_admin -q -c "DROP DATABASE IF EXISTS $SCRATCH_DB" >/dev/null
  exit 0
fi

echo "❌ 有 $DIFF_N 条差异："
echo "---- 只在【新库】有（migrations 多出来的）----"
printf '%s\n' "$ONLY_NEW" | sed '/^$/d' | head -40
echo "---- 只在【目标库】有（migrations 漏掉的）----"
printf '%s\n' "$ONLY_REF" | sed '/^$/d' | head -40
[[ $KEEP -eq 0 ]] && psql_admin -q -c "DROP DATABASE IF EXISTS $SCRATCH_DB" >/dev/null
exit 1
