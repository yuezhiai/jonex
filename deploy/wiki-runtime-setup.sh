#!/usr/bin/env bash
# Persist the selected Wiki profile for subsequent ordinary make up.
set -euo pipefail

platform_dir=${JONEX_PLATFORM_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
deploy_dir="$platform_dir/deploy"
runtime_dir="$deploy_dir/.wiki-runtime"
wiki_env="$deploy_dir/.env.jonex_wiki"

if [ -f "$runtime_dir/compose.yml" ]; then
  echo "Wiki 运行配置已存在：${runtime_dir}；没有覆盖。"
  exit 0
fi
test -f "$wiki_env" || { echo "请先配置 ${wiki_env}。" >&2; exit 1; }
for file in seccomp.json gateway.env settings.env compose.yml; do
  test ! -e "$runtime_dir/$file" || { echo "发现未完成的已有配置：$runtime_dir/$file" >&2; exit 1; }
done

profile=${WIKI_SECCOMP_PROFILE:-}
if [ -z "$profile" ]; then
  config_files=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' jonex-wiki 2>/dev/null || true)
  previous_overlay=${config_files##*,}
  previous_profile="$(dirname "$previous_overlay")/wiki-seccomp.json"
  if [ -n "$previous_overlay" ] && [ -r "$previous_profile" ]; then
    profile="$previous_profile"
  fi
fi
if [ -z "$profile" ] && [ "$(uname -m)" = x86_64 ]; then
  profile="$deploy_dir/wiki-seccomp-x86_64.json"
  python3 - "$profile" <<'PY'
import hashlib, pathlib, sys
path = pathlib.Path(sys.argv[1])
if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != "453f14ed59c1ebb1ffa154614ace09d71090879a87b19b7712a461f03a3af3e5":
    raise SystemExit("项目内 Wiki seccomp 策略校验失败。")
PY
fi
test -n "$profile" && test -r "$profile" || {
  echo '未找到已审查的 Wiki seccomp 配置；请通过 WIKI_SECCOMP_PROFILE 指定。' >&2; exit 1;
}
python3 - "$profile" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    profile = json.load(stream)
if profile.get("defaultAction") not in {"SCMP_ACT_ERRNO", "SCMP_ACT_KILL", "SCMP_ACT_KILL_PROCESS"}:
    raise SystemExit("seccomp 配置不是默认拒绝，停止。")
PY
limit=$(sysctl -n user.max_user_namespaces)
case "$limit" in ''|*[!0-9]*) echo '无法确认用户命名空间上限。' >&2; exit 1;; esac
test "$limit" -ge 64 || { echo '15 并发需要先审查并设置用户命名空间上限，再做沙箱预检。' >&2; exit 1; }

key_line=$(grep -E '^JONEX_WIKI_API_KEY=' "$wiki_env" || true)
[[ "$key_line" =~ ^JONEX_WIKI_API_KEY=[A-Za-z0-9._~+/-]+$ ]] || {
  echo '请在 .env.jonex_wiki 中设置一条非空、单行的 JONEX_WIKI_API_KEY。' >&2; exit 1;
}
umask 077
mkdir -p "$runtime_dir"
cp "$profile" "$runtime_dir/seccomp.json"
printf '%s\n' "$key_line" > "$runtime_dir/gateway.env"
cat > "$runtime_dir/settings.env" <<'EOF'
JONEX_WIKI_API_PARSE_CONCURRENCY=15
JONEX_WIKI_API_COMPILE_CONCURRENCY=15
JONEX_WIKI_COMPILE_CONCURRENCY=15
JONEX_WIKI_BLOCKING_POOL_SIZE=15
JONEX_WIKI_KEEP_BUNDLES=1
TASK_CENTER_SCHEDULER_BATCH=15
EOF
profile_yaml=$(python3 - "$runtime_dir/seccomp.json" <<'PY'
import json, sys
print(json.dumps("seccomp=" + sys.argv[1]))
PY
)

# Docker 默认的 docker-default AppArmor 策略拒绝 mount，bwrap 无法建挂载命名空间，
# 所以宿主启用 AppArmor 时不能用默认策略（seccomp 仍是主边界）。内核带
# apparmor_restrict_unprivileged_userns 开关时（Ubuntu 23.10 起），unconfined 进程新建的
# 用户命名空间会被切到没有任何能力的 unprivileged_userns 策略，bwrap 配置网卡即失败
# （loopback: Failed RTM_NEWADDR）。这类宿主改用 deploy/apparmor/jonex-wiki-sandbox，
# 由 wiki-runtime-ensure.py 安装；不论开关当前取值，以免之后改为 1 时沙箱失效。
apparmor_yaml=""
if [ -r /sys/module/apparmor/parameters/enabled ] && [ "$(cat /sys/module/apparmor/parameters/enabled)" = "Y" ]; then
  if [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
    apparmor_yaml='      - "apparmor=jonex-wiki-sandbox"'
  else
    apparmor_yaml='      - "apparmor=unconfined"'
  fi
fi
# 容器以 root 运行，bwrap 建用户命名空间时把 uid 0 映射为 uid 0。Linux 5.12 起，
# 映射父命名空间的 uid 0 要求创建者持有 CAP_SETFCAP，否则写 uid_map 返回 EPERM
# （bwrap: setting up uid map: Operation not permitted）。只加回这一项能力。
cat > "$runtime_dir/compose.yml" <<EOF
services:
  jonex-wiki:
    security_opt:
      - $profile_yaml
      - "no-new-privileges:true"
${apparmor_yaml:-}
    cap_drop: ["ALL"]
    cap_add: ["SETFCAP"]
    volumes:
      - "/dev/null:/usr/bin/bwrap:ro"
    environment:
      JONEX_WIKI_API_PARSE_CONCURRENCY: "\${JONEX_WIKI_API_PARSE_CONCURRENCY:?}"
      JONEX_WIKI_API_COMPILE_CONCURRENCY: "\${JONEX_WIKI_API_COMPILE_CONCURRENCY:?}"
      JONEX_WIKI_COMPILE_CONCURRENCY: "\${JONEX_WIKI_COMPILE_CONCURRENCY:?}"
      JONEX_WIKI_BLOCKING_POOL_SIZE: "\${JONEX_WIKI_BLOCKING_POOL_SIZE:?}"
      JONEX_WIKI_KEEP_BUNDLES: "\${JONEX_WIKI_KEEP_BUNDLES:?}"
  task-center-service:
    environment:
      TASK_CENTER_SCHEDULER_BATCH: "\${TASK_CENTER_SCHEDULER_BATCH:?}"
  gateway:
    environment:
      JONEX_WIKI_URL: "http://jonex-wiki:8007"
      JONEX_WIKI_API_KEY: "\${JONEX_WIKI_API_KEY:?}"
EOF
echo "Wiki 运行配置已保存到 ${runtime_dir}；之后 make up 会自动加载。"
echo "请在首次启动后运行 Wiki 沙箱预检；调整并发等参数请编辑 settings.env。"
