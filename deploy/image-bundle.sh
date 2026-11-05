#!/usr/bin/env bash
# 镜像离线打包 / 恢复（上传到内网服务器、无外网环境部署用）
#
# 用法（一般通过 Makefile 调用）：
#   bash deploy/image-bundle.sh save [输出目录]     # 本地镜像 → tar 包
#   bash deploy/image-bundle.sh load <目录>         # tar 包 → 本地镜像
#   bash deploy/image-bundle.sh restore             # 用运行中容器找回 <image>:latest 标签
#
# Makefile 入口：
#   make save-images TAG=v1.0.0                     # → dist/images-v1.0.0/
#   make save-images TAG=v1.0.0 INCLUDE_INFRA=1     # 连 postgres/redis/milvus 等第三方镜像一起打包
#   make load-images DIR=dist/images-v1.0.0         # 在目标机器上执行
#
# 环境变量：
#   TAG=v1.0.0        额外记录版本号；load 时自动补打 <image>:v1.0.0（镜像内仍是 :latest）
#   INCLUDE_INFRA=1   连第三方镜像（postgres/redis/milvus/minio/neo4j/etcd）一起打包
#   COMPRESS=gzip     压缩方式：gzip（默认，-1 快速）| none（最快，包最大）| zstd（需 zstd）
#   ONLY=<子串>[,..]  只处理名字包含该子串的镜像
#
# tar 包内是 `docker save` 的原始产物，用本地名（<image>:latest）保存，
# 目标机器 `docker load` 后即恢复成 compose 认识的 :latest，`make up` 直接可用。
#
# 兼容 macOS 自带 bash 3.2。

set -euo pipefail

CALLER_PWD="${PWD}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=lib/compose-images.sh
. "${SCRIPT_DIR}/lib/compose-images.sh"

COMPOSE="${COMPOSE:-docker compose}"
# 与 Makefile 的 COMPOSE_LOCAL_FILES 一致（Darwin 自动带 mac 覆盖）
if [[ "$(uname -s)" == "Darwin" ]]; then
  COMPOSE_ARGS=(-f docker-compose.yml -f docker-compose.mac.yml)
else
  COMPOSE_ARGS=(-f docker-compose.yml)
fi

INCLUDE_INFRA="${INCLUDE_INFRA:-0}"
COMPRESS="${COMPRESS:-gzip}"
VER_TAG="${TAG:-}"
ONLY="${ONLY:-}"

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$@"; }
else
  sha256() { shasum -a 256 "$@"; }
fi

host_arch() {  # 归一化成 docker 的 os/arch 口径
  case "$(uname -m)" in
    x86_64|amd64)   printf 'linux/amd64' ;;
    arm64|aarch64)  printf 'linux/arm64' ;;
    *)              printf 'linux/%s' "$(uname -m)" ;;
  esac
}

HOST_ARCH="${HOST_ARCH:-$(host_arch)}"

match_only() {  # $1=repo
  [[ -z "$ONLY" ]] && return 0
  local pat
  IFS=',' read -r -a _patterns <<< "$ONLY"
  for pat in "${_patterns[@]}"; do
    [[ -z "$pat" ]] && continue
    [[ "$1" == *"$pat"* ]] && return 0
  done
  return 1
}

# 收集要处理的镜像：<repo>|<tag>|<kind>
collect_refs() {
  local svc repo tag kind
  while IFS='|' read -r svc repo tag kind; do
    [[ "$kind" == "infra" && "$INCLUDE_INFRA" != "1" ]] && continue
    match_only "$repo" || continue
    printf '%s|%s\n' "$repo" "$tag"
  done < <(compose_list_images "${COMPOSE_ARGS[@]}")
}

human() {  # 字节 → 人类可读
  awk -v b="$1" 'BEGIN {
    split("B KB MB GB TB", u, " "); i = 1
    while (b >= 1024 && i < 5) { b /= 1024; i++ }
    printf "%.1f%s", b, u[i]
  }'
}

file_size() {  # 跨平台取文件字节数（BSD stat 在前，GNU stat 兜底）
  stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0
}

# docker save 没有进度输出，这里用一个后台探针盯输出文件的增长，
# 每秒刷新「已写入 / 速度 / 用时」；交互终端原地刷新，重定向时按行打印。
save_with_progress() {  # $1=源镜像引用 $2=输出文件 $3=压缩方式
  local src="$1" out="$2" mode="$3"
  local inline=0
  { [[ -t 1 ]] || [[ "${PROGRESS:-0}" == "1" ]]; } && inline=1

  ( case "$mode" in
      none) docker save -o "$out" "$src" ;;
      zstd) docker save "$src" | zstd -q -1 -T0 -o "$out" ;;
      *)    docker save "$src" | gzip -1 > "$out" ;;
    esac ) &
  local pid=$!

  local start elapsed size rate
  start=$(date +%s)
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
    elapsed=$(( $(date +%s) - start ))
    [[ $elapsed -lt 1 ]] && elapsed=1
    size=0
    [[ -f "$out" ]] && size=$(file_size "$out")
    rate=$(( size / elapsed ))
    local line
    if [[ $size -eq 0 ]]; then
      # docker save 要先把层导出成 tar 流，头几秒文件还没长起来，别显示成"卡住"
      line="导出中（准备层）…  用时 ${elapsed}s"
    else
      line="已写入 $(human "$size")  $(human "$rate")/s  用时 ${elapsed}s"
    fi
    if [[ "${PROGRESS:-0}" == "1" ]]; then
      printf '        %s\n' "$line"
    elif [[ $inline -eq 1 ]]; then
      printf '\r        %-64s' "$line"
    fi
  done

  local rc=0
  wait "$pid" || rc=$?
  if [[ $inline -eq 1 && "${PROGRESS:-0}" != "1" ]]; then
    printf '\r%72s\r' ''
  fi
  return "$rc"
}

abs_path() {  # 相对路径按调用者 cwd 解析（脚本内部会 cd 到 deploy/）
  case "$1" in
    /*) printf '%s' "$1" ;;
    *)  printf '%s' "${CALLER_PWD}/$1" ;;
  esac
}

do_save() {
  local out="${1:-}"
  [[ -n "$out" ]] && out="$(abs_path "$out")"
  if [[ -z "$out" ]]; then
    out="${REPO_ROOT}/dist/images-${VER_TAG:-$(date +%Y%m%d-%H%M%S)}"
  fi
  mkdir -p "$out"
  out="$(cd "$out" && pwd)"

  local refs=() n=0
  while IFS='|' read -r repo tag; do
    refs[${#refs[@]}]="${repo}|${tag}"
  done < <(collect_refs)

  if [[ ${#refs[@]} -eq 0 ]]; then
    echo "✗ 没有匹配到任何镜像（ONLY=${ONLY}）" >&2
    exit 1
  fi

  echo "打包到: ${out}"
  echo "镜像数: ${#refs[@]}（INCLUDE_INFRA=${INCLUDE_INFRA}，压缩=${COMPRESS}${VER_TAG:+，版本 tag=${VER_TAG}}）"
  echo

  local saved=0 skipped=0 total=0
  : > "${out}/manifest.txt"

  for entry in "${refs[@]}"; do
    local repo="${entry%%|*}" tag="${entry##*|}" ref="${entry%%|*}:${entry##*|}"
    local src="${repo}:${tag}"
    if ! docker image inspect "$src" >/dev/null 2>&1; then
      printf '  - %-46s 本地没有，跳过\n' "$src"
      skipped=$((skipped + 1))
      continue
    fi
    n=$((n + 1))
    local base fname file
    base="$(printf '%s' "$ref" | tr '/:' '__')"
    n_pad="$(printf '%03d' "$n")"
    case "$COMPRESS" in
      none) fname="${n_pad}-${base}.tar"; file="${out}/${fname}" ;;
      zstd) fname="${n_pad}-${base}.tar.zst"; file="${out}/${fname}" ;;
      *)    fname="${n_pad}-${base}.tar.gz";  file="${out}/${fname}" ;;
    esac
    local src_size
    src_size="$(human "$(docker image inspect --format '{{.Size}}' "$src")")"
    printf '  [%d/%d] %-46s（源镜像 %s）→ %s\n' "$n" "${#refs[@]}" "$src" "$src_size" "$fname"
    if [[ "$COMPRESS" == "zstd" ]] && ! command -v zstd >/dev/null 2>&1; then
      echo "    ✗ 没有 zstd 命令（macOS: brew install zstd），改用 COMPRESS=gzip" >&2
      exit 2
    fi
    local started size
    started=$(date +%s)
    if ! save_with_progress "$src" "$file" "$COMPRESS"; then
      echo "    ✗ docker save 失败：$src" >&2
      rm -f "$file"
      exit 1
    fi
    size=$(file_size "$file")
    total=$((total + size))
    printf '        完成 %s（%ss）\n' "$(human "$size")" "$(( $(date +%s) - started ))"
    img_arch="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$src")"
    printf '%s|%s|%s|%s\n' "$ref" "$VER_TAG" "$fname" "$img_arch" >> "${out}/manifest.txt"
    [[ "$img_arch" != "$HOST_ARCH" ]] && arch_note="${img_arch}"
    saved=$((saved + 1))
  done

  if [[ $saved -eq 0 ]]; then
    echo "✗ 一个镜像都没打包成功" >&2
    exit 1
  fi

  # 校验和 + 加载脚本 + 说明
  ( cd "$out" && sha256 ./*.tar ./*.tar.gz ./*.tar.zst 2>/dev/null | sed 's#\./##' > SHA256SUMS ) || true
  cat > "${out}/load.sh" <<'EOS'
#!/usr/bin/env bash
# 在目标机器上执行：恢复全部镜像，并按 manifest 补打版本 tag
set -euo pipefail
cd "$(dirname "$0")"
echo "== 校验文件完整性"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum -c SHA256SUMS
else
  shasum -a 256 -c SHA256SUMS
fi
echo "== 导入镜像"
for f in *.tar *.tar.gz *.tar.zst; do
  [ -e "$f" ] || continue
  echo "-- docker load -i $f"
  docker load -i "$f"
done
echo "== 补打版本 tag"
while IFS='|' read -r ref ver _file; do
  [ -z "${ver:-}" ] && continue
  base="${ref%%:*}"
  docker tag "${base}:latest" "${base}:${ver}" 2>/dev/null && echo "   $base:$ver"
done < manifest.txt
echo "完成。下一步: make up（或 make up-server）"
EOS
  chmod +x "${out}/load.sh"

  cat > "${out}/README.txt" <<EOF
Jonex 平台镜像离线包
====================
版本 tag : ${VER_TAG:-（未指定）}
镜像数量 : ${saved}（跳过 ${skipped}）
压缩方式 : ${COMPRESS}
总大小   : $(human "$total")
打包机架构: ${HOST_ARCH}

传到内网服务器（二选一）:
  rsync -avP "$(basename "$out")/" user@server:/opt/jonex/images/
  tar -cf - -C "$(dirname "$out")" "$(basename "$out")" | ssh user@server 'tar -xf - -C /opt/jonex'

在服务器上恢复:
  cd /opt/jonex/images/$(basename "$out")
  ./load.sh
  # 然后（在代码目录）:
  #   make up-server        # 生产档
  #   make up               # 本地档

说明: 包内镜像按本地名 <image>:latest 保存，docker load 后 compose 可直接使用。
EOF

  echo
  echo "完成：${saved} 个镜像，共 $(human "$total")，跳过 ${skipped} 个"
  echo "打包机架构: ${HOST_ARCH}$( [[ -n "${arch_note:-}" ]] && printf '（⚠️  其中有 %s 镜像，与打包机架构不同）' "${arch_note}" )"
  echo "目标服务器架构必须与包内镜像一致，否则 docker run 会 exec format error（uname -m 自查）"
  echo "目录: ${out}"
  echo "下一步：把整个目录传到服务器，在服务器上执行 ${out}/load.sh"
}

do_load() {
  local dir="${1:-}"
  [[ -n "$dir" ]] && dir="$(abs_path "$dir")"
  if [[ -z "$dir" || ! -d "$dir" ]]; then
    echo "用法: bash deploy/image-bundle.sh load <目录>" >&2
    exit 2
  fi
  dir="$(cd "$dir" && pwd)"
  echo "从 ${dir} 导入镜像"
  if [[ -f "${dir}/SHA256SUMS" ]]; then
    echo "== 校验完整性"
    ( cd "$dir" && sha256 -c SHA256SUMS )
  fi
  if [[ -f "${dir}/manifest.txt" ]]; then
    local arch_in_tar _r _v _f
    while IFS='|' read -r _r _v _f arch_in_tar; do
      [[ -z "${arch_in_tar:-}" ]] && continue
      if [[ "$arch_in_tar" != "$HOST_ARCH" ]]; then
        echo "⚠️  包内镜像架构 ${arch_in_tar}，本机 ${HOST_ARCH} —— 镜像能导入但可能无法运行（exec format error）"
        break
      fi
    done < "${dir}/manifest.txt"
  fi
  local loaded=0
  for f in "${dir}"/*.tar "${dir}"/*.tar.gz "${dir}"/*.tar.zst; do
    [[ -e "$f" ]] || continue
    printf '  docker load -i %s\n' "$(basename "$f")"
    docker load -i "$f"
    loaded=$((loaded + 1))
  done
  if [[ -f "${dir}/manifest.txt" ]]; then
    local ref ver _file base
    while IFS='|' read -r ref ver _file _arch; do
      [[ -z "${ver:-}" ]] && continue
      base="${ref%%:*}"
      docker tag "${base}:latest" "${base}:${ver}" 2>/dev/null && echo "  补打 ${base}:${ver}"
    done < "${dir}/manifest.txt"
  fi
  echo "完成：导入 ${loaded} 个 tar 包"
  echo "下一步: make up（本地）/ make up-server（生产）"
}

# 非破坏性校验：tar 包校验和 + 包内记录的镜像 ID 是否与本地镜像一致
do_verify() {
  local dir="${1:-}"
  [[ -n "$dir" ]] && dir="$(abs_path "$dir")"
  if [[ -z "$dir" || ! -d "$dir" ]]; then
    echo "用法: bash deploy/image-bundle.sh verify <目录>" >&2
    exit 2
  fi
  dir="$(cd "$dir" && pwd)"
  local ok=0 bad=0 missing=0
  echo "校验目录: ${dir}"
  if [[ -f "${dir}/SHA256SUMS" ]]; then
    echo "== 文件完整性"
    ( cd "$dir" && sha256 -c SHA256SUMS ) || { echo "✗ SHA256SUMS 校验失败（传输损坏）" >&2; exit 1; }
  else
    echo "  （没有 SHA256SUMS，跳过文件校验）"
  fi
  echo "== 包内镜像 ID vs 本地镜像 ID"
  local ref ver file id_in_tar id_local
  while IFS='|' read -r ref ver file arch_in_tar; do
    [[ -z "$ref" ]] && continue
    local tar_info
    tar_info="$(python3 "$SCRIPT_DIR/lib/tar-image-id.py" "${dir}/${file}" 2>/dev/null)" || tar_info=""
    id_in_tar="${tar_info%%|*}"
    local tag_in_tar="${tar_info##*|}"
    if [[ -z "$id_in_tar" ]]; then
      echo "  ✗ ${file} 读不出镜像 ID"
      bad=$((bad + 1))
      continue
    fi
    if [[ -n "$tag_in_tar" && "$tag_in_tar" != "$ref" ]]; then
      printf '  ! %-46s 包内镜像名是 %s（manifest 记的是 %s）\n' "$file" "$tag_in_tar" "$ref"
    fi
    id_local="$(docker image inspect --format '{{.Id}}' "${ref}" 2>/dev/null | sed 's/^sha256://')" || id_local=""
    if [[ -z "$id_local" ]]; then
      printf '  - %-46s 本地没有该镜像（只有包，属正常）\n' "$ref"
      missing=$((missing + 1))
    elif [[ "$id_in_tar" == "$id_local" ]]; then
      printf '  ✓ %-46s ID 一致（%s，%s）\n' "$ref" "${id_in_tar:0:12}" "${arch_in_tar:-未知架构}"
      ok=$((ok + 1))
    else
      printf '  ✗ %-46s ID 不一致！包内 %s / 本地 %s\n' "$ref" "${id_in_tar:0:12}" "${id_local:0:12}"
      bad=$((bad + 1))
    fi
  done < "${dir}/manifest.txt"
  echo
  echo "校验结果：一致 ${ok} 个，不一致 ${bad} 个，本地缺失 ${missing} 个"
  if [[ $bad -gt 0 ]]; then
    exit 1
  fi
  echo "包是好的：可以传服务器，服务器上执行 $(basename "$dir")/load.sh"
}

# 误删 tag 后的补救：用 compose 容器当前使用的镜像 ID 找回 <image>:latest
do_restore() {
  echo "从运行中/已停止的 compose 容器找回 <image>:latest 标签"
  local restored=0
  local cid img iid
  for cid in $(docker ps -aq); do
    local proj
    proj=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$cid" 2>/dev/null || true)
    [[ "$proj" == "deploy" ]] || continue
    img=$(docker inspect --format '{{.Config.Image}}' "$cid" 2>/dev/null || true)
    iid=$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || true)
    [[ -z "$img" || -z "$iid" ]] && continue
    case "$img" in *@*|*:* ) continue ;; esac
    if docker image inspect "${img}:latest" >/dev/null 2>&1; then
      continue
    fi
    docker tag "${iid#sha256:}" "${img}:latest" && { echo "  ✓ ${img}:latest"; restored=$((restored + 1)); }
  done
  echo "恢复 ${restored} 个标签（没有容器的镜像无法这样恢复，需重建或从制品仓库拉）"
}

case "${1:-}" in
  save)    shift; do_save "${1:-}" ;;
  load)    shift; do_load "${1:-}" ;;
  verify)  shift; do_verify "${1:-}" ;;
  restore) do_restore ;;
  *)
    echo "用法: bash deploy/image-bundle.sh save [输出目录] | load <目录> | verify <目录> | restore" >&2
    echo "      make save-images TAG=v1.0.0 [INCLUDE_INFRA=1]" >&2
    echo "      make load-images DIR=dist/images-v1.0.0" >&2
    exit 2
    ;;
esac
