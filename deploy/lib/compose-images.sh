#!/usr/bin/env bash
# compose 镜像清单解析（tag-images.sh / image-bundle.sh 共用，单一事实来源）
#
# 用法（在脚本里 source 后调用）:
#   . "$SCRIPT_DIR/lib/compose-images.sh"
#   compose_list_images -f docker-compose.yml -f docker-compose.mac.yml
#
# 输出每行: <service>|<repo>|<tag>|<kind>
#   kind = build  该服务在 compose 里有 build:（本项目自己构建的镜像）
#        = infra  第三方镜像（postgres/redis/milvus/...）
#
# 约定:
# - 统一带 `--profile '*'`：`docker compose config` 默认会过滤掉挂了 profiles 的服务
#   （如 identity-sync-gateway），不加就会漏镜像。
# - 镜像名取 services.<svc>.image；万一新增服务漏写 image:，
#   按 compose 默认规则回落成 `<project>-<service>`。
# - 兼容 macOS 自带 bash 3.2。

compose_list_images() {
  local cfg
  cfg="$(${COMPOSE:-docker compose} "$@" --profile '*' config --format json)"
  printf '%s' "$cfg" | python3 -c '
import json, sys

cfg = json.load(sys.stdin)
project = cfg.get("name") or "deploy"
for svc, spec in sorted(cfg.get("services", {}).items()):
    kind = "build" if spec.get("build") else "infra"
    ref = spec.get("image")
    if not ref:
        if kind == "infra":
            continue
        ref = "%s-%s" % (project, svc)
    head = ref.rsplit("/", 1)[-1]
    if ":" in head:
        repo, tag = ref.rsplit(":", 1)
    else:
        repo, tag = ref, "latest"
    print("%s|%s|%s|%s" % (svc, repo, tag, kind))
'
}
