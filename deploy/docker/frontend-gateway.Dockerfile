FROM nginx:1.27-alpine

RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone

# 业务路由（两种接入模式共用）与候选监听配置。
# 均放在 /etc/nginx/conf.d/ 之外——那个目录被 nginx.conf 在 http 层自动加载，
# 而这些文件是 server / location 层指令，放进去会直接报配置错误。
# default.conf 不再随镜像烘死，由启动时的 05-access-mode.sh 按 ACCESS_MODE 生成。
COPY deploy/nginx/app-locations.conf /etc/nginx/app-locations.conf
COPY deploy/nginx/modes/ /etc/nginx/modes/

# 5xx 静态错误页（app-locations.conf 的 error_page 指向它）。
# 覆盖基础镜像自带的同名文件，上游全挂时由本容器直接返回，不反代给 shell。
COPY deploy/nginx/50x.html /usr/share/nginx/html/50x.html

# 接入模式选择器：编号 05，必须早于官方 10-listen-on-ipv6-by-default.sh，
# 否则它写的 default.conf 会覆盖掉官方脚本的 IPv6 补丁。
COPY deploy/docker/frontend-gateway-mode.sh /docker-entrypoint.d/05-access-mode.sh
RUN chmod +x /docker-entrypoint.d/05-access-mode.sh

# CSP 片段生成器：nginx 官方 entrypoint 会在启动前按序执行 /docker-entrypoint.d/*.sh，
# 由它按 CSP_STORAGE_ORIGIN 生成 /etc/nginx/csp.conf（被 app-locations.conf include）。
COPY deploy/docker/frontend-gateway-csp.sh /docker-entrypoint.d/40-csp.sh
RUN chmod +x /docker-entrypoint.d/40-csp.sh

EXPOSE 80 443

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/health || exit 1

CMD ["nginx", "-g", "daemon off;"]
