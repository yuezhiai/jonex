#!/bin/sh
# ============================================================
# 按 ACCESS_MODE 选择对外接入形态，写入 /etc/nginx/conf.d/default.conf
#
#   ACCESS_MODE=http （默认）—— IP + HTTP，无 TLS。公司内部部署
#   ACCESS_MODE=https        —— 域名 + HTTPS，TLS 在本容器终止。公网部署
#
# 只做「选哪份配置」，不做文本插值：两份候选配置保持纯静态可审阅，
# 且不会误伤 nginx 自身的运行时变量（$host / $uri / $request_id ...）。
# 官方 templates/envsubst 机制会替换配置里所有 $VAR，包括这些运行时变量，
# 会静默改坏反代——这是刻意不用它的原因。
#
# 脚本编号 05：排在官方 /docker-entrypoint.d/ 各脚本之前，让后续脚本看到的
# 已经是最终配置。注意官方的 10-listen-on-ipv6-by-default.sh 不会给我们的配置
# 补 IPv6 监听——它只处理 checksum 与镜像自带版本一致的 default.conf，我们写入的
# 必然不一致（实测输出 "differs from the packaged version" 后跳过）。这与改造前
# 的 frontend-gateway.conf 行为一致，IPv6 监听此前也未启用，非回退。
#
# 方案：docs/https-domain-access-plan.md
# ============================================================

set -e

MODE=$(echo "${ACCESS_MODE:-http}" | tr 'A-Z' 'a-z')
TARGET=/etc/nginx/conf.d/default.conf
CERT_DIR=/etc/nginx/certs

case "$MODE" in
    http)
        cp -f /etc/nginx/modes/server-http.conf "$TARGET"
        echo "[OK] 接入模式 http —— 监听 80，无 TLS（IP 直接访问）"
        ;;
    https)
        # 提前校验证书，让失败点明确。否则 nginx 只会抛一句 cannot load certificate，
        # 还得自己去猜是路径、挂载、文件名还是权限问题。
        for f in fullchain.crt private.key; do
            if [ ! -f "$CERT_DIR/$f" ]; then
                echo "[ERROR] ACCESS_MODE=https 但证书缺失：$CERT_DIR/$f" >&2
                echo "[ERROR] 请确认 deploy/.env 的 TLS_CERT_DIR 指向宿主机证书目录，" >&2
                echo "[ERROR] 且该目录下存在 fullchain.crt 与 private.key（文件名固定）。" >&2
                exit 1
            fi
        done
        cp -f /etc/nginx/modes/server-https.conf "$TARGET"
        echo "[OK] 接入模式 https —— 80 跳转、443 终止 TLS"
        ;;
    *)
        echo "[ERROR] ACCESS_MODE=$MODE 非法，有效值：http | https" >&2
        exit 1
        ;;
esac
