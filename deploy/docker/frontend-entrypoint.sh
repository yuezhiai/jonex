#!/bin/sh
# ============================================================
# Jonex 平台前端 — 运行时配置注入
# 替换 index.html 中的 __JONEX_CONFIG_PLACEHOLDER__ 为实际环境变量
#
# 注意不再注入 API_BASE_URL：前端各 api 模块的 baseURL 是写死的相对路径 /api/v1
# （shared-lib/src/api/request.ts 默认值），相对路径自动跟随页面 scheme 与 host，
# 这正是 IP+HTTP / 域名+HTTPS 两种接入形态都能工作的原因。原先注入的
# FRONTEND_API_BASE_URL 无任何消费者，且其值 http://gateway:8000 是容器内主机名，
# 浏览器解析不了。整条链路已按 docs/https-domain-access-plan.md 5.9 删除。
# ============================================================

set -e

ENV="${ENV:-production}"
APP_TITLE="${APP_TITLE:-Jonex 平台}"

CONFIG="{\"ENV\": \"${ENV}\", \"APP_TITLE\": \"${APP_TITLE}\"}"

INDEX_FILE="${INDEX_FILE:-/usr/share/nginx/html/index.html}"

if [ -f "$INDEX_FILE" ]; then
    sed -i "s|__JONEX_CONFIG_PLACEHOLDER__|${CONFIG}|g" "$INDEX_FILE"
    echo "[OK] Config injected into ${INDEX_FILE}"
else
    echo "[WARN] ${INDEX_FILE} not found, skipping config injection"
fi

exec nginx -g "daemon off;"
