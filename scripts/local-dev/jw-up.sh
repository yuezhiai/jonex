#!/usr/bin/env bash
# ============================================================
# jonex_wiki 链路：日常启动 + 按代码改动增量重建
# ------------------------------------------------------------
# 设计前提（别改坏）：
#   1) 只 build 需要重建的服务，且**永远不加 --no-cache**（复用镜像层 + BuildKit 缓存）
#   2) 只 up 链路最小集，其余链路外服务 stop 掉（unless-stopped 对显式 stop 是粘的）
#   3) 绝不 down -v / --rmi / prune（会毁掉镜像与数据卷）
#
# 用法：
#   scripts/local-dev/jw-up.sh                   # 只起链路（不重建）
#   scripts/local-dev/jw-up.sh --changed         # 按 git 改动增量重建后再起
#   scripts/local-dev/jw-up.sh --rebuild jonex-wiki gateway
#   scripts/local-dev/jw-up.sh --no-ui           # 只起 API（不启前端）
#   scripts/local-dev/jw-up.sh --rebuild-base    # 额外重建 jonex/python-base:local
#   scripts/local-dev/jw-up.sh --dry-run --changed
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.mac.yml -f docker-compose.override.yml)
compose() { (cd deploy && docker compose "${COMPOSE_FILES[@]}" "$@"); }

# 链路必需（含 UI）：frontend-gateway 会带上 4 个前端 + gateway，
# jonex-wiki 会带上 postgres + llm-gateway；gateway->sidecar->三个业务服务。
UP_WITH_UI=(frontend-gateway jonex-wiki)
UP_NO_UI=(gateway jonex-wiki)

# 链路不需要的（只 stop 一次即长期粘住；atomic-rag 只有上传新文档才需要）
UNUSED=(milvus etcd minio lightrag openkb atomic-rag mcp-server)

DRY=0; DO_CHANGED=0; WITH_UI=1; REBUILD_BASE=0; EXPLICIT=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)      DRY=1 ;;
    --changed)      DO_CHANGED=1 ;;
    --no-ui)        WITH_UI=0 ;;
    --rebuild-base) REBUILD_BASE=1 ;;
    --rebuild)      shift; while [[ $# -gt 0 && "$1" != --* ]]; do EXPLICIT+=("$1"); shift; done; continue ;;
    -h|--help)      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

run() { if [[ $DRY -eq 1 ]]; then echo "  [dry-run] $*"; else "$@"; fi; }

# ---------- 1) 确保 Docker daemon ----------
if [[ $DRY -eq 0 ]]; then
  if ! docker info >/dev/null 2>&1; then
    echo "==> Docker daemon 未运行，尝试 orb start"
    orb start >/dev/null 2>&1 || true
    for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 2; done
    docker info >/dev/null 2>&1 || { echo "!! daemon 起不来，请手动开 OrbStack 后重跑" >&2; exit 1; }
  fi
  echo "==> Docker daemon OK"
fi

# ---------- 2) 算出要重建的服务 ----------
MARKER="$ROOT/.tmp-setup/jw-up.last"
declare -a TO_BUILD=()

map_changed() {   # stdin: 变更文件列表 -> stdout: 需重建的服务名
  local f
  while read -r f; do
    [[ -z "$f" ]] && continue
    case "$f" in
      Reference/JonexWiki/*)            echo jonex-wiki ;;
      jonex_core/*)                     echo "jonex-wiki gateway sidecar knowledge-base-service platform-service business-domain-service" ;;
      deploy/*)                         echo "jonex-wiki gateway sidecar knowledge-base-service platform-service business-domain-service frontend-gateway" ;;
      api_gateway/*|run_gateway.py)     echo gateway ;;
      capabilities/*)                   echo "gateway knowledge-base-service platform-service business-domain-service" ;;
      main.py)                          echo sidecar ;;
      frontends/shell/*)                echo shell-frontend ;;
      frontends/core-business/*)        echo core-business-frontend ;;
      frontends/platform-management/*)  echo platform-management-frontend ;;
      frontends/ecosystem-management/*) echo ecosystem-management-frontend ;;
    esac
  done | tr ' ' '\n' | sort -u | grep -v '^$' || true
}

if [[ ${#EXPLICIT[@]} -gt 0 ]]; then
  TO_BUILD=("${EXPLICIT[@]}")
elif [[ $DO_CHANGED -eq 1 ]]; then
  SINCE=""
  [[ -f "$MARKER" ]] && SINCE="$(cat "$MARKER")"
  if [[ -n "$SINCE" ]] && git rev-parse --verify -q "$SINCE^{commit}" >/dev/null 2>&1; then
    echo "==> 对比 $SINCE..HEAD 的代码改动"
    # macOS 自带 bash 3.2 没有 mapfile，用 while-read 保持 3.2 兼容
    TO_BUILD=()
    while IFS= read -r svc; do
      [[ -n "$svc" ]] && TO_BUILD+=("$svc")
    done < <(git diff --name-only "$SINCE" HEAD | map_changed)
  else
    echo "==> 无基线（标记文件缺失/无效）-> 保守重建链路全部后端"
    TO_BUILD=(jonex-wiki gateway sidecar knowledge-base-service platform-service business-domain-service)
  fi
fi

if [[ $REBUILD_BASE -eq 1 ]]; then
  echo "==> 重建共享基础镜像 jonex/python-base:local（有缓存，通常很快）"
  run make build-python-base
fi

if [[ ${#TO_BUILD[@]} -gt 0 ]]; then
  echo "==> 增量重建（cached，不用 --no-cache）：${TO_BUILD[*]}"
  run compose build "${TO_BUILD[@]}"
else
  echo "==> 无需重建（直接用现有镜像，不触发任何 build）"
fi

# ---------- 3) 起链路最小集 ----------
if [[ $WITH_UI -eq 1 ]]; then TARGETS=("${UP_WITH_UI[@]}"); else TARGETS=("${UP_NO_UI[@]}"); fi
echo "==> 启动链路：${TARGETS[*]}（compose 自动补齐 postgres/redis/llm-gateway/sidecar 等依赖）"
run compose up -d "${TARGETS[@]}"

# ---------- 4) 停掉链路外服务（保持停止；不删容器、不删镜像） ----------
echo "==> 停掉链路外服务：${UNUSED[*]}"
run compose stop "${UNUSED[@]}" >/dev/null 2>&1 || true

# ---------- 5) 等健康 ----------
if [[ $DRY -eq 0 ]]; then
  echo "==> 等待健康检查"
  for c in jonex-postgres jonex-redis jonex-llm-gateway jonex-sidecar jonex-knowledge-base jonex-gateway jonex-wiki; do
    st=missing
    for _ in $(seq 1 60); do
      st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null || echo missing)"
      [[ "$st" == healthy || "$st" == running ]] && break
      sleep 2
    done
    printf '  %-26s %s\n' "$c" "$st"
  done

  # ---------- 6) 记录基线 + 打印入口 ----------
  mkdir -p "$(dirname "$MARKER")"
  git rev-parse HEAD > "$MARKER" 2>/dev/null || true
  # 入口里的 KB id 现场查（脚本不写死某个实例的 id）；查不到就退回占位符。
  kb_id="$(docker exec jonex-postgres psql -U jonex -d jonex -tAc \
    "select id from knowledge_base.knowledge_info where kb_type='jonex_wiki' and is_deleted=0 order by created_at limit 1;" \
    2>/dev/null | tr -d '[:space:]')"
  [ -n "$kb_id" ] || kb_id='<KB_ID>'
  echo
  echo "入口："
  echo "  界面  : http://localhost/core-business/domain-knowledge/$kb_id/compile-results"
  echo "  网关  : http://localhost:8000/docs        Sidecar : http://localhost:8001/docs"
  # 注：知识库的容器并行 REST 层（/api/v1/knowledge-info 等 84 条）已于 2026-10
  # 渗透测试整改中物理删除，故 8003/docs 现在只列出 /invoke 与 /health —— 直连调试
  # 请用 POST /invoke（需内部服务 JWT），或走网关前缀 /api/v1/knowledge-base/**。
  echo "  知识库: http://localhost:8003/docs        （仅 /invoke；REST 层已删除）"
  echo "  引擎  : docker exec -it jonex-wiki sh"
  echo "  日志  : docker logs -f jonex-wiki        （或 make logs-service SERVICE=jonex-wiki）"
  echo "  基线已记录: $MARKER（下次 --changed 从这里开始算）"
fi
