#!/bin/sh
# ============================================================
# 生成 CSP 响应头片段 —— 被 deploy/nginx/app-locations.conf 的 include 引入
#
# 为什么用「生成单文件 + include」而不是 nginx 官方镜像的 templates/envsubst：
#   envsubst 会替换配置里所有 $VAR，包括 nginx 自身的运行时变量
#   （$host / $uri / $proxy_add_x_forwarded_for ...），会静默改坏反代。
#   官方 NGINX_ENVSUBST_FILTER 能限制范围，但依赖后人维护时同步更新过滤规则。
#   这里把变量插值收敛到本脚本的 heredoc 内，配置主体零改动，无此风险。
#
# 本脚本放在 /docker-entrypoint.d/，由 nginx 官方 entrypoint 在启动前自动执行，
# 不接管 ENTRYPOINT、不自己 exec nginx，官方初始化流程保持原样。
#
# CSP_STORAGE_ORIGIN：对象存储的来源（scheme://host），供 img-src / media-src
#   直连使用。未设置时按 OBJECT_STORAGE_BACKEND 取默认值：
#   - cos   → https://*.myqcloud.com（COS 域固定，故无需配置，保持既有部署零改动）
#   - s3    → 推不出来，告警提示显式配置。因为 AWS 的 host 含 region
#             （https://*.s3.<region>.amazonaws.com，virtual-hosted 风格，
#             bucket 名占一级子域），而 MinIO 是自建域（https://minio.example.com，
#             path 风格，host 即 S3_ENDPOINT_URL 的 host），两者都无法从后端名推导
#   - local → 留空。local 后端不产生预签名 URL，原文一律从共享卷同源返回
#   不确定填什么时，预览一次文档，看 gateway 日志「原文预览: ... upstream=」字段。
#
# 只有 img-src / media-src 需要这个来源：图片与音视频走 302 直跳存储（Range
# 流式最优）。PDF / 文本 / Markdown 走网关同源代理（proxy=1），属于 'self'。
# 注意：若将来引入浏览器直传（fetch PUT 到预签名 URL），connect-src 必须一并
# 加上 ${ORIGIN}，否则直传会被 CSP 拦截。
# ============================================================

set -e

ORIGIN="${CSP_STORAGE_ORIGIN:-}"
BACKEND=$(echo "${OBJECT_STORAGE_BACKEND:-local}" | tr 'A-Z' 'a-z')
CSP_FILE=/etc/nginx/csp.conf

if [ -z "$ORIGIN" ]; then
    case "$BACKEND" in
        cos)
            ORIGIN="https://*.myqcloud.com"
            echo "[INFO] CSP_STORAGE_ORIGIN 未设置，按 OBJECT_STORAGE_BACKEND=cos 取默认 ${ORIGIN}"
            ;;
        s3)
            echo "[WARN] OBJECT_STORAGE_BACKEND=s3 但 CSP_STORAGE_ORIGIN 未设置。"
            echo "[WARN] 图片/音视频预览走 302 直连对象存储，其来源无法从后端名推导"
            echo "[WARN] （AWS 的 host 含 region，MinIO 是自建域），请在 .env 显式配置，"
            echo "[WARN] 否则 CSP 由 Report-Only 转为强制模式后，浏览器会拦截直连预览。"
            ;;
    esac
fi

cat > "$CSP_FILE" <<EOF
add_header Content-Security-Policy-Report-Only "default-src 'self'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: ${ORIGIN}; media-src 'self' blob: ${ORIGIN}; frame-src 'self' blob:; font-src 'self' data:; connect-src 'self';" always;
EOF

echo "[OK] CSP generated at ${CSP_FILE} (storage origin: ${ORIGIN:-<none, same-origin only>})"
