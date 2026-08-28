FROM nginx:1.27-alpine

RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone

COPY deploy/nginx/frontend-gateway.conf /etc/nginx/conf.d/default.conf

# CSP 片段生成器：nginx 官方 entrypoint 会在启动前按序执行 /docker-entrypoint.d/*.sh，
# 由它按 CSP_STORAGE_ORIGIN 生成 /etc/nginx/csp.conf（被 default.conf include）。
COPY deploy/docker/frontend-gateway-csp.sh /docker-entrypoint.d/40-csp.sh
RUN chmod +x /docker-entrypoint.d/40-csp.sh

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/health || exit 1

CMD ["nginx", "-g", "daemon off;"]
