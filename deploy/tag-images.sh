#!/usr/bin/env bash
# 给「本 compose 工程会构建的镜像」追加自定义 tag（原 tag 保持不动），
# 可选带制品仓库前缀，便于直接 push。
#
# 用法（一般通过 Makefile 调用，少直接跑）：
#   bash deploy/tag-images.sh <tag> [-f docker-compose.yml ...]
#   REGISTRY=registry.example.com bash deploy/tag-images.sh <tag>   # 带仓库前缀
#   PUSH=1 REGISTRY=... bash deploy/tag-images.sh <tag>                        # 打标后推送
#
# Makefile 入口：
#   make build TAG=v1.0.0                                    # 只打本地 jonex-gateway:v1.0.0
#   make build TAG=v1.0.0 REGISTRY=registry.example.com
#   make push-tag TAG=v1.0.0 REGISTRY=registry.example.com
#
# 只做 docker tag：镜像 ID 不变，不会重复构建；:latest 仍然保留，所以
# `make up` 的行为完全不受影响（它按 compose 里的 image 名走，默认 latest）。
#
# 镜像名来源：`docker compose config` 解析出的 services.<svc>.image
# （compose 里已为每个构建服务显式声明 jonex-* 名字；万一以后新增服务漏写 image:，
#  这里按 compose 默认规则回落成 `<project>-<service>`，即 deploy-<service>）。
#
# 兼容 macOS 自带 bash 3.2：不用 mapfile / 关联数组。

set -euo pipefail

NEW_TAG="${1:-}"
if [[ -z "$NEW_TAG" ]]; then
  echo "用法: bash deploy/tag-images.sh <tag> [-f docker-compose.yml ...]" >&2
  echo "      可选环境变量: REGISTRY=<仓库前缀> PUSH=1" >&2
  exit 2
fi
if [[ "$NEW_TAG" == "latest" ]]; then
  echo "✗ 不要用 latest 作为自定义 tag（它就是现有 tag）" >&2
  exit 2
fi
case "$NEW_TAG" in
  *[!A-Za-z0-9_.-]* | .* | -*)
    echo "✗ 非法 tag: ${NEW_TAG}（只允许字母/数字/下划线/点/中划线，且不能以 . 或 - 开头）" >&2
    exit 2
    ;;
esac
shift

# 制品仓库前缀，如 registry.example.com（不带结尾 /、不带 http://）
REGISTRY="${REGISTRY:-}"
REGISTRY="${REGISTRY%/}"
PUSH="${PUSH:-0}"
if [[ -n "$REGISTRY" ]]; then
  case "$REGISTRY" in
    http://* | https://*)
      echo "✗ REGISTRY 不要带 http(s):// 前缀: ${REGISTRY}" >&2
      exit 2
      ;;
    *[[:space:]]*)
      echo "✗ REGISTRY 不能含空格: ${REGISTRY}" >&2
      exit 2
      ;;
  esac
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE="${COMPOSE:-docker compose}"
# 默认组合与 Makefile 的 COMPOSE_LOCAL_FILES 一致（Darwin 自动带 mac 覆盖）
if [[ $# -eq 0 ]]; then
  set -- -f docker-compose.yml
  if [[ "$(uname -s)" == "Darwin" ]]; then
    set -- -f docker-compose.yml -f docker-compose.mac.yml
  fi
fi

# --profile '*'：把挂了 profiles 的服务（如 identity-sync-gateway）也纳入，
# 否则 `docker compose config` 会把它们过滤掉，导致这些镜像永远拿不到版本 tag / 仓库名。
# shellcheck source=lib/compose-images.sh
. "${SCRIPT_DIR}/lib/compose-images.sh"

REFS=()
while IFS='|' read -r _svc repo tag kind; do
  [[ "$kind" == "build" ]] || continue
  [[ -n "$repo" ]] && REFS[${#REFS[@]}]="$repo|$tag"
done < <(compose_list_images "$@")

if [[ ${#REFS[@]} -eq 0 ]]; then
  echo "✗ 没有解析到任何会构建的服务（compose 文件参数对吗？）" >&2
  exit 1
fi

# 实时跑一条命令：有终端时用 pty（docker 才会画逐层进度条），
# 同时把输出落盘用于失败取因；无终端（CI/日志重定向）退化为普通流式。
# PUSH_TTY=1 强制 pty，PUSH_TTY=0 强制普通流式（默认按 stdout 是否终端判断）。
_run_live() {  # $1=日志文件，其余=命令
  local log="$1"; shift
  local use_tty="${PUSH_TTY:-auto}"
  if [[ "$use_tty" == "auto" ]]; then
    if [[ -t 1 ]]; then use_tty=1; else use_tty=0; fi
  fi
  if [[ "$use_tty" == "1" ]]; then
    python3 - "$log" "$@" <<'PY' && return 0 || return $?
import os, pty, sys

log_path, cmd = sys.argv[1], sys.argv[2:]
log = open(log_path, "wb")


def pump(fd):
    ok = True
    while True:
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        try:
            sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
        except Exception:
            ok = False
        log.write(data); log.flush()
    return ok


pid, fd = pty.fork()
if pid == 0:
    try:
        os.execvp(cmd[0], cmd)
    except Exception:
        os._exit(127)
pump(fd)
os.close(fd)
_, status = os.waitpid(pid, 0)
log.close()
code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
sys.exit(0 if code == 0 else 1)
PY
  else
    "${@}" >"$log" 2>&1 &
    local pid=$!
    tail -f "$log" 2>/dev/null &
    local tailpid=$!
    wait "$pid"; local rc=$?
    kill "$tailpid" 2>/dev/null
    wait "$tailpid" 2>/dev/null
    return "$rc"
  fi
}

# ---------- 拉取模式：从制品仓库拉回镜像并落成本地名 ----------
# PULL=1 REGISTRY=<repo-slug> bash deploy/tag-images.sh <tag>
# 拉 <REGISTRY>/<image>:<tag>，成功后打本地 <image>:<tag> 和 <image>:latest，
# 所以之后 `make up` / `make up-server` 直接用拉下来的镜像（:latest 被覆盖）。
if [[ "${PULL:-0}" == "1" ]]; then
  if [[ -z "$REGISTRY" ]]; then
    echo "✗ PULL=1 需要同时给 REGISTRY（制品仓库前缀）" >&2
    exit 2
  fi
  ONLY="${ONLY:-}"
  total=0
  for entry in "${REFS[@]}"; do
    repo="${entry%%|*}"
    if [[ -n "$ONLY" ]]; then
      matched=0
      IFS=',' read -r -a ONLY_PARTS <<< "$ONLY"
      for pat in "${ONLY_PARTS[@]}"; do
        [[ -z "$pat" ]] && continue
        [[ "$repo" == *"$pat"* ]] && { matched=1; break; }
      done
      [[ $matched -eq 1 ]] || continue
    fi
    total=$((total + 1))
  done
  echo "从 ${REGISTRY} 拉取 ${total} 个镜像 :${NEW_TAG} …（私有仓库需先 docker login ${REGISTRY%%/*}）"
  os_arch="$(uname -s)/$(uname -m)"
  pulled=0
  failed=0
  first_error=""
  idx=0
  for entry in "${REFS[@]}"; do
    repo="${entry%%|*}"
    if [[ -n "$ONLY" ]]; then
      matched=0
      IFS=',' read -r -a ONLY_PARTS <<< "$ONLY"
      for pat in "${ONLY_PARTS[@]}"; do
        [[ -z "$pat" ]] && continue
        [[ "$repo" == *"$pat"* ]] && { matched=1; break; }
      done
      [[ $matched -eq 1 ]] || continue
    fi
    idx=$((idx + 1))
    ref="${REGISTRY}/${repo}:${NEW_TAG}"
    printf '\n▶ [%d/%d] %s\n' "$idx" "$total" "$ref"
    log="$(mktemp -t jonex-pull.XXXXXX)"
    started=$(date +%s)
    if _run_live "$log" docker pull "$ref"; then
      # 落成本地名：compose 里的 image 是 <repo>:latest，所以同时打 :TAG 和 :latest
      docker tag "$ref" "${repo}:${NEW_TAG}"
      if [[ "$NEW_TAG" != "latest" ]]; then
        docker tag "$ref" "${repo}:latest"
      fi
      printf '✓ 完成 %s -> 本地 %s:%s（%ss）\n' "$ref" "$repo" "$NEW_TAG" "$(( $(date +%s) - started ))"
      pulled=$((pulled + 1))
      rm -f "$log"
      continue
    fi
    reason="$(tr '\r' '\n' <"$log" | grep -vE '^[[:space:]]*$' \
      | grep -viE '(Pulling|Waiting|Downloading|Download complete|Extracting|Pull complete|Verifying Checksum|Already exists|Retrying|^[0-9a-f]{12}:)' \
      | tail -1)"
    [[ -z "$reason" ]] && reason="$(tr '\r' '\n' <"$log" | grep -vE '^[[:space:]]*$' | tail -1)"
    printf '✗ 失败 %s\n    原因: %s\n' "$ref" "$reason"
    failed=$((failed + 1))
    [[ -z "$first_error" ]] && first_error="$reason"
    rm -f "$log"
    if [[ "${KEEP_GOING:-0}" != "1" ]]; then
      echo
      echo "✗ 首个失败即停止（剩余 $((total - idx)) 个未尝试）。" >&2
      echo "  常见原因: 没 docker login / tag 不存在 / 镜像架构与本机不同（当前 ${os_arch}）。" >&2
      break
    fi
  done
  echo
  echo "拉取结果：成功 ${pulled} 个，失败 ${failed} 个"
  if [[ $failed -gt 0 ]]; then
    [[ -n "$first_error" ]] && echo "  首个失败原因: ${first_error}"
    echo "  排查方向: docker login ${REGISTRY%%/*}；确认该 tag 已 push（make push-tag TAG=...）；"
    echo "            镜像架构需与运行机器一致（Mac/ARM 构建的镜像不能直接在 x86 服务器跑）。"
    exit 1
  fi
  echo "下一步: make up（本地）  或  make up-server（服务器）"
  exit 0
fi

if [[ -n "$REGISTRY" ]]; then
  echo "给 ${#REFS[@]} 个镜像追加 tag: ${NEW_TAG}（同时打仓库名 ${REGISTRY}/<image>:${NEW_TAG}）"
else
  echo "给 ${#REFS[@]} 个镜像追加 tag: ${NEW_TAG}"
fi

tagged=0
skipped=0
REMOTE_REFS=()
# ONLY=<子串[,子串]>：只处理匹配的镜像（调试 / 单镜像推送用）
ONLY="${ONLY:-}"
for entry in "${REFS[@]}"; do
  repo="${entry%%|*}"
  src_tag="${entry##*|}"
  src="${repo}:${src_tag}"
  if [[ -n "$ONLY" ]]; then
    matched=0
    IFS=',' read -r -a ONLY_PARTS <<< "$ONLY"
    for pat in "${ONLY_PARTS[@]}"; do
      [[ -z "$pat" ]] && continue
      [[ "$repo" == *"$pat"* ]] && { matched=1; break; }
    done
    [[ $matched -eq 1 ]] || continue
  fi
  if ! docker image inspect "$src" >/dev/null 2>&1; then
    printf '  - %-52s 未构建，跳过\n' "$src"
    skipped=$((skipped + 1))
    continue
  fi
  docker tag "$src" "${repo}:${NEW_TAG}"
  if [[ -n "$REGISTRY" ]]; then
    remote="${REGISTRY}/${repo}:${NEW_TAG}"
    docker tag "$src" "$remote"
    REMOTE_REFS[${#REMOTE_REFS[@]}]="$remote"
    printf '  ✓ %-46s -> %s\n' "$repo" "$remote"
  else
    printf '  ✓ %-52s -> :%s\n' "$repo" "$NEW_TAG"
  fi
  tagged=$((tagged + 1))
done

echo "完成：${tagged} 个镜像已打上 :${NEW_TAG}，跳过 ${skipped} 个"


if [[ "$PUSH" == "1" ]]; then
  if [[ -z "$REGISTRY" ]]; then
    echo "✗ PUSH=1 需要同时给 REGISTRY（推送目标仓库前缀）" >&2
    exit 2
  fi
  total=${#REMOTE_REFS[@]}
  echo
  echo "推送 ${total} 个镜像到 ${REGISTRY} …（需要 docker login ${REGISTRY%%/*}）"
  pushed=0
  failed=0
  first_error=""
  idx=0
  for ref in "${REMOTE_REFS[@]}"; do
    idx=$((idx + 1))
    printf '\n▶ [%d/%d] %s\n' "$idx" "$total" "$ref"
    log="$(mktemp -t jonex-push.XXXXXX)"
    started=$(date +%s)
    if _run_live "$log" docker push "$ref"; then
      printf '✓ 完成 %s（%ss）\n' "$ref" "$(( $(date +%s) - started ))"
      pushed=$((pushed + 1))
      rm -f "$log"
      continue
    fi
    # 取真实报错行（进度条/逐层状态行都是噪音）
    reason="$(tr '\r' '\n' <"$log" | grep -vE '^[[:space:]]*$' \
      | grep -viE '(Preparing|Waiting|Pushing|Pushed|Layer already exists|Mounted from|Retrying|^[0-9a-f]{12}:)' \
      | tail -1)"
    [[ -z "$reason" ]] && reason="$(tr '\r' '\n' <"$log" | grep -vE '^[[:space:]]*$' | tail -1)"
    printf '✗ 失败 %s\n    原因: %s\n' "$ref" "$reason"
    failed=$((failed + 1))
    [[ -z "$first_error" ]] && first_error="$reason"
    rm -f "$log"
    # 鉴权/权限类错误对每个镜像都一样，继续推只会重复失败：默认立刻停下
    if [[ "${KEEP_GOING:-0}" != "1" ]]; then
      echo
      echo "✗ 首个失败即停止（剩余 $((total - idx)) 个未尝试）。确认凭据/仓库路径后重试；" >&2
      echo "  确需全部尝试可加 KEEP_GOING=1。" >&2
      break
    fi
  done
  echo
  echo "推送结果：成功 ${pushed} 个，失败 ${failed} 个"
  if [[ $failed -gt 0 ]]; then
    [[ -n "$first_error" ]] && echo "  首个失败原因: ${first_error}"
    echo "  排查方向: docker login ${REGISTRY%%/*}（令牌需 registry-package 读写、使用范围覆盖目标）；"
    echo "            账号在目标代码仓库的角色需「开发者」及以上；"
    echo "            REGISTRY 必须指到「代码仓库 slug」（如 registry.example.com/jonex-platform），"
    echo "            只写到组（如 registry.example.com）会得到 unauthorized: requested access to the resource is denied。"
    exit 1
  fi
fi

if [[ $tagged -gt 0 ]]; then
  echo
  echo "查看本地结果: docker images --format '{{.Repository}}:{{.Tag}}' | grep ':${NEW_TAG}$'"
fi
