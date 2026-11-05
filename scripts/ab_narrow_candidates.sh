#!/usr/bin/env bash
# S3 窄触发 A/B（在 125 评测机上跑；本机没有 Docker/BIRD 库，跑不了）
#
# 目的：验证 `NL2SQL_CANDIDATE_NARROW` 收窄候选触发后，能否在
#       「准确率 ≥ +2pp、p50 < 1.3×」两条门上同时过（交接文档 2026-10-30 第一优先级）。
#
# 三臂（同一语料、同一模型、同一最佳组合）：
#   base    : 最佳组合，CANDIDATES=0（参照，看候选机制整体值不值得）
#   wide    : 最佳组合 + CANDIDATES=2（S3 首轮的宽触发，p50 2.4× 那个）
#   narrow  : 最佳组合 + CANDIDATES=2 + CANDIDATE_NARROW=1（本次要验的）
#
# 用法（在 125 上，仓库根或任意目录）：
#   REPO=/home/user/jonex-xgb \
#   CORPUS=/home/user/bird/corpus_smoke \
#   FEWSHOT_PATH=/home/user/bird/fewshot_dev.json \
#   bash scripts/ab_narrow_candidates.sh
#
# CORPUS 可以是单个 YAML，也可以是一个目录（目录内 5 个库的 *.yaml 逐个跑、
# 结果合并成 $OUT_DIR/<arm>.json；分库统计靠合并时写入的 `source` 字段）。
#
# 评测在 KB 容器内跑（control 真值要连数据源登记的 `postgres` 容器名，宿主机解析不了）。
# 语料与 tests/ 会 docker cp 进容器；结果 docker cp 回宿主机。
#
# 先决条件：
#   - 125 已 checkout 到含 `NL2SQL_CANDIDATE_NARROW` 的提交（f0274920b 或其后）；
#   - deploy/.env 可写；脚本会**备份并在退出时恢复**（含 trap）；
#   - 评测语料是 run_eval.py 认的 YAML（`--corpus`）。
#
# 结束后：
#   - 三份 JSON + md 在 $OUT_DIR；
#   - 用 scripts/ab_compare.py 做配对比较：
#       python scripts/ab_compare.py $OUT_DIR/wide.json $OUT_DIR/narrow.json \
#           --label-control wide --label-fix narrow
#       python scripts/ab_compare.py $OUT_DIR/base.json $OUT_DIR/narrow.json \
#           --label-control base --label-fix narrow

set -euo pipefail

# 125 上 uv 装在 ~/.local/bin（非交互 ssh 的 PATH 通常不含它）。
export PATH="$HOME/.local/bin:$PATH"

REPO=${REPO:-/home/user/jonex-xgb}
ENV_FILE=${ENV_FILE:-$REPO/deploy/.env}
# 语料：单个 YAML，或一个目录（目录内所有 *.yaml 逐个跑、结果合并）。
CORPUS=${CORPUS:-$REPO/eval_data/bird/corpus_smoke}
if [[ ! -e "$CORPUS" && -d /home/user/bird/corpus_smoke ]]; then
  CORPUS=/home/user/bird/corpus_smoke
fi
OUT_DIR=${OUT_DIR:-$REPO/eval_data/bird/bird_ab_narrow}
GATEWAY=${GATEWAY:-http://gateway:8000}   # 容器内跑评测：走 compose 服务名
TENANT=${TENANT:-tenant_jonex_demo}
USER_NAME=${USER_NAME:-admin}
PASSWORD=${PASSWORD:-admin123}
MODEL=${MODEL:-deepseek-v4-flash-202605}
REPEAT=${REPEAT:-3}
KB_CONTAINER=${KB_CONTAINER:-jonex-knowledge-base}
FEWSHOT_PATH=${FEWSHOT_PATH:-/home/user/bird/fewshot_dev.json}
PY=${PY:-"uv run python"}

# 最佳组合（交接文档口径：examples + value + preview + v2 示例值 + projection
# + rank + hint_exactness + few-shot + 4s + 4t；评测 cap 10000）。
# 可用额外环境变量覆盖：BASE_FLAGS_EXTRA="NL2SQL_XXX=1"。
BASE_FLAGS=(
  "NL2SQL_MAX_ROWS=10000"
  "NL2SQL_HINTS=1"
  "NL2SQL_COLUMN_EXAMPLES=1"
  "NL2SQL_VALUE_LOOKUP=1"
  "NL2SQL_SCHEMA_PREVIEW=1"
  "NL2SQL_PREVIEW_EXAMPLES=1"
  "NL2SQL_PROJECTION_CONTRACT=1"
  "NL2SQL_RANK_HINT=1"
  "NL2SQL_HINT_EXACTNESS=1"
  "NL2SQL_FEWSHOT=1"
  "NL2SQL_FEWSHOT_PATH=$FEWSHOT_PATH"
  "NL2SQL_SINGLE_COLUMN_HINT=1"
  "NL2SQL_DATE_CAST_HINT=1"
)
if [[ -n "${BASE_FLAGS_EXTRA:-}" ]]; then
  # shellcheck disable=SC2206
  BASE_FLAGS+=(${BASE_FLAGS_EXTRA})
fi

BACKUP="$ENV_FILE.ab_bak.$$"
APPLIED=0

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "找不到 $ENV_FILE（REPO 指对了吗？）"
[[ -e "$CORPUS" ]] || die "找不到语料 $CORPUS（用 CORPUS=... 指定）"
command -v docker >/dev/null || die "没有 docker"

restore_env() {
  if [[ "$APPLIED" == 1 && -f "$BACKUP" ]]; then
    mv -f "$BACKUP" "$ENV_FILE"
    log "已恢复 deploy/.env 备份（$BACKUP）"
    rebuild_kb || true
    local left
    left=$(grep -c '^NL2SQL_' "$ENV_FILE" 2>/dev/null || true)
    log "恢复后 deploy/.env 中 NL2SQL_* 行数：${left:-0}"
    if [[ "${left:-0}" != "0" ]]; then
      log "⚠️ 原 .env 本就带 NL2SQL_* 配置；已按原样恢复，未擅自清空。"
    fi
  fi
}
trap restore_env EXIT INT TERM

apply_flags() {
  local -a flags=("$@")
  if [[ "$APPLIED" == 0 ]]; then
    cp -f "$ENV_FILE" "$BACKUP"
    APPLIED=1
  fi
  { grep -v '^NL2SQL_' "$ENV_FILE" || true; } > "$ENV_FILE.tmp.$$"
  printf '%s\n' "${flags[@]}" >> "$ENV_FILE.tmp.$$"
  mv -f "$ENV_FILE.tmp.$$" "$ENV_FILE"
  log "已写入 ${#flags[@]} 个 NL2SQL_* 开关"
}

wait_healthy() {
  local status
  for _ in $(seq 1 120); do
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$KB_CONTAINER" 2>/dev/null || echo missing)
    [[ "$status" == "healthy" ]] && return 0
    sleep 5
  done
  die "容器 $KB_CONTAINER 未在 10 分钟内 healthy（最后状态：$status）"
}

rebuild_kb() {
  log "重建并重启 $KB_CONTAINER"
  ( cd "$REPO/deploy" && docker compose up -d --build knowledge-base-service )
  wait_healthy
}

prepare_eval() {
  # 评测**在 KB 容器内跑**：control 真值要连数据源里登记的 host（通常是容器名
  # `postgres`），宿主机解析不了；容器内既解析得动 postgres，也解析得动 gateway。
  docker exec "$KB_CONTAINER" mkdir -p /app/_ab/corpus
  docker cp "$REPO/tests" "$KB_CONTAINER:/app/tests" >/dev/null
  if [[ -d "$CORPUS" ]]; then
    docker cp "$CORPUS/." "$KB_CONTAINER:/app/_ab/corpus/" >/dev/null
  else
    docker cp "$CORPUS" "$KB_CONTAINER:/app/_ab/corpus/" >/dev/null
  fi
}

run_arm() {
  local name=$1
  shift
  apply_flags "$@"
  rebuild_kb
  prepare_eval
  mkdir -p "$OUT_DIR/$name"
  docker exec "$KB_CONTAINER" mkdir -p "/app/_ab/$name"
  local corpus base
  while IFS= read -r corpus; do
    base=$(basename "${corpus%.yaml}")
    log "跑 $name / $base（repeat=$REPEAT，容器内）"
    docker exec "$KB_CONTAINER" python -m tests.eval.nl2sql.run_eval \
      --corpus "/app/_ab/corpus/$(basename "$corpus")" \
      --out "/app/_ab/$name/$base.json" \
      --repeat "$REPEAT" \
      --gateway "$GATEWAY" \
      --tenant "$TENANT" \
      --user "$USER_NAME" \
      --password "$PASSWORD" \
      --model "$MODEL" \
      --hints
    docker cp "$KB_CONTAINER:/app/_ab/$name/$base.json" "$OUT_DIR/$name/$base.json" >/dev/null
  done < <(collect_corpora)
  merge_reports "$name"
}

collect_corpora() {
  if [[ -d "$CORPUS" ]]; then
    find "$CORPUS" -maxdepth 1 \( -name '*.yaml' -o -name '*.yml' \) | sort
  else
    printf '%s\n' "$CORPUS"
  fi
}

merge_reports() {
  local name=$1
  python3 - "$OUT_DIR/$name" "$OUT_DIR/$name.json" <<'PYEOF'
import json
import pathlib
import sys

src = pathlib.Path(sys.argv[1])
dst = pathlib.Path(sys.argv[2])
merged = {"run_at": "", "repeat": 0, "model": "", "hints": True,
          "results": [], "summary": {}, "official_summary": {}, "coverage": {}}
for path in sorted(src.glob("*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    source = (data.get("target") or {}).get("name") or path.stem
    for row in data.get("results") or []:
        row.setdefault("source", source)
    merged["results"].extend(data.get("results") or [])
    merged["repeat"] = max(merged["repeat"], int(data.get("repeat") or 0))
    merged["model"] = data.get("model") or merged["model"]
    merged["hints"] = data.get("hints", merged["hints"])
    merged["run_at"] = merged["run_at"] or (data.get("run_at") or "")
dst.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"[merge] {len(merged['results'])} cases -> {dst}")
PYEOF
}

mkdir -p "$OUT_DIR"

run_arm base   "${BASE_FLAGS[@]}"
run_arm wide   "${BASE_FLAGS[@]}" "NL2SQL_CANDIDATES=2"
run_arm narrow "${BASE_FLAGS[@]}" "NL2SQL_CANDIDATES=2" "NL2SQL_CANDIDATE_NARROW=1"

log "全部完成。报告：$OUT_DIR"
log "配对比较（官方口径为主）："
log "  python3 scripts/ab_compare.py $OUT_DIR/wide.json $OUT_DIR/narrow.json --label-control wide --label-fix narrow"
log "  python3 scripts/ab_compare.py $OUT_DIR/base.json $OUT_DIR/narrow.json --label-control base --label-fix narrow"
