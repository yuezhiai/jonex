# syntax=docker/dockerfile:1
FROM node:24.22.1-alpine AS builder
WORKDIR /app

# 构建源镜像开关：false=国内源（npmmirror），true=国外官方源（registry.npmjs.org）
ARG USE_OVERSEAS_MIRROR=false

RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone
RUN if [ "$USE_OVERSEAS_MIRROR" = "true" ]; then \
        npm config set registry https://registry.npmjs.org; \
    else \
        npm config set registry https://registry.npmmirror.com; \
    fi \
    && npm config set fund false && npm config set audit false
RUN npm install -g pnpm@9
RUN pnpm config set store-dir /pnpm-store

# Workspace metadata — only the 3 packages needed for this sub-app
COPY frontends/pnpm-workspace.yaml ./
COPY frontends/pnpm-lock.yaml ./
RUN echo '{"name":"jonex-frontend","private":true}' > package.json

COPY frontends/shared/shell-sdk/package.json ./shared/shell-sdk/
COPY frontends/shared/platform-theme/package.json ./shared/platform-theme/
COPY frontends/shared/i18n-resources/package.json ./shared/i18n-resources/
COPY frontends/shared/shared-lib/package.json ./shared/shared-lib/
COPY frontends/platform-management/package.json ./platform-management/

RUN --mount=type=cache,id=jonex-pnpm-store,target=/pnpm-store \
    pnpm install

# Source for build — only the 5 packages needed
COPY frontends/shared/shell-sdk/ ./shared/shell-sdk/
COPY frontends/shared/platform-theme/ ./shared/platform-theme/
COPY frontends/shared/i18n-resources/ ./shared/i18n-resources/
COPY frontends/shared/shared-lib/ ./shared/shared-lib/
COPY frontends/platform-management/ ./platform-management/
# 工程化基础配置（tsconfig.base / vite.base 位于 frontends 根）
COPY frontends/tsconfig.base.json ./
COPY frontends/vite.base.config.ts ./

# 生产登录页为同源相对路径 /login；覆盖 .env 烘入的本地 dev 网关地址 localhost:8080（生产无 8080 服务，跳转会落到死地址）
# 构建期注入的生产值：dev 的 `.env` 已从构建上下文排除（.dockerignore），
# 这些键原先全靠 `.env` 提供，缺了会退回代码默认值（standalone 路由基址错、appId 错）。
ARG VITE_LOGIN=/login
ARG VITE_APP_ID=platform-management
ARG VITE_STANDALONE_BASE=/platform-management/
ARG VITE_AUTH_ME=/api/v1/auth/me
ARG VITE_AUTH_EXCHANGE_TICKET=/api/v1/auth/exchange-ticket
ENV VITE_LOGIN=${VITE_LOGIN} \
    VITE_APP_ID=${VITE_APP_ID} \
    VITE_STANDALONE_BASE=${VITE_STANDALONE_BASE} \
    VITE_AUTH_ME=${VITE_AUTH_ME} \
    VITE_AUTH_EXCHANGE_TICKET=${VITE_AUTH_EXCHANGE_TICKET}

RUN pnpm --filter @jonex/platform-management build

FROM nginx:1.27-alpine
# ARG 不跨 stage 继承：最终镜像的 label 要在本 stage 重新声明才能取到值
ARG USE_OVERSEAS_MIRROR=false
LABEL org.jonex.build.mirror="${USE_OVERSEAS_MIRROR}"
WORKDIR /usr/share/nginx/html
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone

COPY --from=builder /app/platform-management/dist ./platform-management/
COPY frontends/platform-management/nginx/default.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://127.0.0.1/health || exit 1
CMD ["nginx", "-g", "daemon off;"]
