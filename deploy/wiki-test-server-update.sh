#!/usr/bin/env bash
# Update the existing test-server Wiki deployment without changing its checkout.
set -euo pipefail

platform_dir=${JONEX_PLATFORM_DIR:-/home/app/jonex-platform}
releases_dir=${JONEX_RELEASES_DIR:-/home/app/jonex-wiki-releases}
target=${1:-}

if [[ ! "$target" =~ ^[0-9a-f]{40}$ ]]; then
  echo '用法: bash wiki-test-server-update.sh <40 位平台提交 SHA>' >&2
  exit 2
fi
cd "$platform_dir"
git cat-file -e "$target^{commit}"

config_files=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' jonex-wiki)
previous_overlay=${config_files##*,}
case "$previous_overlay" in
  "$releases_dir"/*/compose-release.yml) ;;
  *) echo '未找到当前 Wiki 的发布覆盖配置，停止更新。' >&2; exit 1 ;;
esac
previous_dir=$(dirname "$previous_overlay")
previous=$(cat "$previous_dir/source/RELEASE_COMMIT")
test -f "$previous_dir/wiki-seccomp.json"
test -f "$previous_dir/gateway-wiki.env"
test -f "$previous_dir/preflight-links.py"
test "$(sysctl -n user.max_user_namespaces)" = 64

if [ "$previous" = "$target" ]; then
  echo "服务器已经使用该发布版本：$target"
  exit 0
fi
git merge-base --is-ancestor "$previous" "$target" || {
  echo '目标版本不包含当前服务器版本，停止更新。' >&2; exit 1;
}
if ! git diff --quiet "$previous" "$target" -- \
    deploy/docker-compose.yml deploy/docker-compose.override.yml deploy/postgres/update \
    deploy/docker/python-base.Dockerfile \
    frontends; then
  echo '目标版本包含基础依赖、解析器、部署配置、数据库增量或前端更新，请先人工核对更新方式。' >&2
  exit 1
fi

active_tasks() {
  docker exec jonex-postgres psql -U jonex -d jonex -At -c \
    "SELECT count(*) FROM task_center.tasks WHERE status IN ('running','pending','queued','processing')"
}
test "$(active_tasks)" = 0 || { echo '仍有任务在运行或排队，请等任务结束后再更新。' >&2; exit 1; }

short=${target:0:9}
release="$releases_dir/wiki-manual-$(date +%Y%m%d-%H%M%S)-$short"
umask 077
mkdir -p "$release/source"
git archive "$target" | tar -xf - -C "$release/source"
printf '%s\n' "$target" > "$release/source/RELEASE_COMMIT"
cp "$previous_dir/wiki-seccomp.json" "$previous_dir/gateway-wiki.env" "$previous_dir/preflight-links.py" "$release/"

for name in wiki kb task sidecar gateway; do
  grep -q "image: jonex-$name-release:" "$previous_overlay" || {
    echo "当前覆盖配置缺少 $name 镜像，停止更新。" >&2; exit 1;
  }
done
sed -e "s|$previous_dir/|$release/|g" \
    -e "s|jonex-wiki-release:[^[:space:]]*|jonex-wiki-release:$short|" \
    -e "s|jonex-kb-release:[^[:space:]]*|jonex-kb-release:$short|" \
    -e "s|jonex-task-release:[^[:space:]]*|jonex-task-release:$short|" \
    -e "s|jonex-sidecar-release:[^[:space:]]*|jonex-sidecar-release:$short|" \
    -e "s|jonex-gateway-release:[^[:space:]]*|jonex-gateway-release:$short|" \
    "$previous_overlay" > "$release/compose-release.yml"

echo "准备更新平台：$target"
cd "$release/source"
export DOCKER_BUILDKIT=1
docker build --build-arg USE_OVERSEAS_MIRROR=false -f deploy/docker/jonex-wiki.Dockerfile -t "jonex-wiki-release:$short" .
docker build --build-arg CAPABILITY_NAME=knowledge_base --build-arg PYTHON_BASE=jonex/python-base:local -f deploy/docker/capability.Dockerfile -t "jonex-kb-release:$short" .
docker build --build-arg CAPABILITY_NAME=task_center --build-arg PYTHON_BASE=jonex/python-base:local -f deploy/docker/capability.Dockerfile -t "jonex-task-release:$short" .
docker build --build-arg PYTHON_BASE=jonex/python-base:local -f deploy/docker/sidecar.Dockerfile -t "jonex-sidecar-release:$short" .
docker build --build-arg PYTHON_BASE=jonex/python-base:local -f deploy/docker/gateway.Dockerfile -t "jonex-gateway-release:$short" .

docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
  --security-opt "seccomp=$release/wiki-seccomp.json" --tmpfs /tmp:rw,exec,nosuid,nodev,size=256m \
  -v /dev/null:/usr/bin/bwrap:ro -v "$release/preflight-links.py:/checks/preflight-links.py:ro" \
  --entrypoint python "jonex-wiki-release:$short" /checks/preflight-links.py | tee "$release/preflight.log"
grep -qx 'preflight True' "$release/preflight.log"
grep -q 'write=DENIED' "$release/preflight.log"

cd "$platform_dir"
compose=(docker compose --env-file deploy/.env -f deploy/docker-compose.yml -f deploy/docker-compose.override.yml)
"${compose[@]}" -f "$release/compose-release.yml" config -q
test "$(active_tasks)" = 0 || { echo '构建期间有新任务开始，请等待完成后重新运行。' >&2; exit 1; }
echo "切换服务；如需回滚，使用覆盖文件：$previous_overlay"
"${compose[@]}" -f "$release/compose-release.yml" up -d --no-build --no-deps \
  jonex-wiki knowledge-base-service task-center-service sidecar gateway
echo "已切换至 ${target}；检查服务状态："
docker ps --format '{{.Names}} {{.Status}}' | grep -E '^jonex-(wiki|knowledge-base|task-center|sidecar|gateway) '
echo "发布目录：$release"
echo "此前的覆盖文件和镜像仍保留：$previous_overlay"
