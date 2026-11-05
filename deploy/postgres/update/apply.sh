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
#   JONEX_PG_SKIP_FILE  全新库豁免清单（仅离线安装传入；清单内脚本会登记为已应用
#                       但不执行——它们依赖已被后续版本删除的旧结构）
#   JONEX_PG_DISABLE_STRUCTURAL_SKIP=1  关掉「结构前提自动豁免」（排障用：强制按原样
#                       执行全部待应用脚本，报错照旧中断）
#
# 结构前提自动豁免（默认开启）：
#   少数历史脚本依赖的旧结构已被后续版本删除（例如 `task_catalog_steps.task_catalog_code`
#   被 `080_task_center_drop_catalog.sql` 删掉）。这类脚本在**已是新结构**的库上必然报错，
#   但它们的用途（历史数据修复）对新结构库没有意义。判据只看**库的真实结构**：
#     · 旧结构还在（存量库尚未演进到删除那一版）⇒ 照常执行，修复照旧生效
#     · 旧结构已不在（全新库 / 已升级库）        ⇒ 登记为已应用并跳过
#   这样 `make db-migrate` 在全新库上不再需要人工记住传 JONEX_PG_SKIP_FILE。
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

环境变量: JONEX_PG_CONTAINER / JONEX_PG_USER / JONEX_PG_DB / JONEX_PG_SKIP_FILE
          JONEX_PG_DISABLE_STRUCTURAL_SKIP=1（关掉结构前提自动豁免，排障用）

结构前提自动豁免（默认开启）：依赖旧结构（已被后续版本删除）的历史脚本，
在新结构库上会登记为已应用并跳过，不再必然失败。判据只看库的真实结构。
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

# ---- 只读标量查询（结构前提检查用；失败返回空串，不中断脚本）----
psql_scalar() {
  docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tA -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null || true
}

# ---- 结构前提探测（只查一次，结果供 structural_skip_reason 复用）----
TC_CATALOG_STATE="table_missing"   # table_missing | present | dropped
structural_probe() {
  [[ "${JONEX_PG_DISABLE_STRUCTURAL_SKIP:-0}" == "1" ]] && return 0
  local state
  state="$(psql_scalar "
    SELECT CASE
             WHEN to_regclass('task_center.task_catalog_steps') IS NULL THEN 'table_missing'
             WHEN EXISTS (SELECT 1
                            FROM information_schema.columns
                           WHERE table_schema = 'task_center'
                             AND table_name   = 'task_catalog_steps'
                             AND column_name  = 'task_catalog_code') THEN 'present'
             ELSE 'dropped'
           END;")"
  # 查询失败（容器/权限问题）时保持 table_missing ⇒ 不豁免，让后续执行按原样暴露错误
  case "$state" in
    present|dropped) TC_CATALOG_STATE="$state" ;;
    *)               TC_CATALOG_STATE="table_missing" ;;
  esac
}

# 返回 0 并打印原因 = 该脚本的结构前提不满足，应登记为已应用并跳过
structural_skip_reason() {
  [[ "${JONEX_PG_DISABLE_STRUCTURAL_SKIP:-0}" == "1" ]] && return 1
  case "$1" in
    # 三份都要求 task_center.task_catalog_steps.task_catalog_code 存在。
    # 080 下线内置任务层后该列被整体删除，列不在 = 脚本必然报 column does not exist。
    # 注意只认 `dropped`（表在、列没了）：表都不在说明基线 migrations/ 还没跑过，
    # 这时不能豁免，得让 036 自己建表、把问题暴露出来。
    036_task_center.sql|044_task_center_jonex_wiki.sql|054_task_center_preset_repair.sql)
      if [[ "$TC_CATALOG_STATE" == "dropped" ]]; then
        echo "task_center.task_catalog_steps.task_catalog_code 已不存在（update/080 已下线内置任务层）"
        return 0
      fi
      ;;
  esac
  return 1
}

# ---- bootstrap 版本表 ----
psql_exec -q <<'SQL'
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename   VARCHAR(255) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

# ---- 探测库的真实结构（决定哪些历史脚本可以安全豁免）----
structural_probe

# ---- 读取已应用文件列表 ----
applied_file="$(mktemp)"
trap 'rm -f "$applied_file"' EXIT
docker exec -i "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tA \
  -c "SELECT filename FROM public.schema_migrations;" > "$applied_file"

# ---- 收集待应用 / 已应用 / 结构豁免 ----
# 豁免的脚本不执行，但仍要登记为已应用（否则下次还会被列成待应用）。
# 登记动作延迟到 dry-run 判定之后，保证 --dry-run 不写库。
shopt -s nullglob
pending=()
to_register=()
skipped=0
structural_skipped=0
for f in "${SCRIPT_DIR}"/*.sql; do
  name="$(basename "$f")"
  if grep -qxF "$name" "$applied_file"; then
    echo "SKIP  ${name}（已应用）"
    skipped=$((skipped + 1))
  elif [ -n "${JONEX_PG_SKIP_FILE:-}" ] && [ -f "${JONEX_PG_SKIP_FILE}" ] \
       && grep -qxF "$name" "${JONEX_PG_SKIP_FILE}"; then
    # 全新库豁免：依赖存量库历史结构的脚本，登记为已应用但不执行
    echo "SKIP  ${name}（全新库豁免，将登记为已应用）"
    skipped=$((skipped + 1))
    to_register+=("$name")
  elif skip_reason="$(structural_skip_reason "$name")"; then
    # 结构前提不满足（旧结构已被后续版本删除）：登记为已应用并跳过，避免必然失败
    echo "SKIP  ${name}（结构前提不满足，将登记为已应用）"
    echo "        ↳ ${skip_reason}"
    skipped=$((skipped + 1))
    structural_skipped=$((structural_skipped + 1))
    to_register+=("$name")
  else
    pending+=("$f")
  fi
done

# ---- 先把豁免项登记掉（dry-run 不写）----
register_exempt() {
  local n
  # 注意 bash 3.2（macOS 自带）：空数组直接用 "${arr[@]}" 在 set -u 下会报 unbound，
  # 用 ${arr[@]+"${arr[@]}"} 惯用法兜住。
  for n in ${to_register[@]+"${to_register[@]}"}; do
    psql_exec -q -c "INSERT INTO public.schema_migrations (filename) VALUES ('${n}') ON CONFLICT (filename) DO NOTHING;" 2>/dev/null || true
  done
}

if [[ ${#pending[@]} -eq 0 ]]; then
  [[ $DRY_RUN -eq 0 && ${#to_register[@]} -gt 0 ]] && register_exempt
  echo "==> 无待应用迁移（共 ${skipped} 个已处理，其中结构豁免 ${structural_skipped} 个）。"
  exit 0
fi

echo "==> 待应用迁移 ${#pending[@]} 个："
for f in "${pending[@]}"; do
  echo "    - $(basename "$f")"
done
if [[ $structural_skipped -gt 0 ]]; then
  echo "==> 另有 ${structural_skipped} 个因结构前提不满足被跳过。"
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "==> [dry-run] 未执行任何迁移，也未登记任何豁免项。"
  exit 0
fi

if [[ $ASSUME_YES -eq 0 ]]; then
  read -r -p "确认执行以上 ${#pending[@]} 个迁移？[y/N] " answer
  case "${answer:-}" in
    y|Y|yes|Yes|YES) : ;;
    *) echo "已取消，未执行任何迁移。"; exit 0 ;;
  esac
fi

# 豁免项先登记（和执行失败无关，可安全提前落库）
[[ ${#to_register[@]} -gt 0 ]] && register_exempt

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

skip_suffix=""
[[ $structural_skipped -gt 0 ]] && skip_suffix="（另结构豁免 ${structural_skipped} 个）"
echo "==> 增量 DDL 应用完成：本次 ${#pending[@]} 个${skip_suffix}。"
