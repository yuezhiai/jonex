# syntax=docker/dockerfile:1
FROM node:20.18-alpine AS builder
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

# Workspace metadata — only the 4 packages needed for shell
COPY frontends/pnpm-workspace.yaml ./
COPY frontends/pnpm-lock.yaml ./
RUN echo '{"name":"jonex-frontend","private":true}' > package.json

COPY frontends/shared/shell-sdk/package.json ./shared/shell-sdk/
COPY frontends/shared/platform-theme/package.json ./shared/platform-theme/
COPY frontends/shared/i18n-resources/package.json ./shared/i18n-resources/
COPY frontends/shared/shared-lib/package.json ./shared/shared-lib/
COPY frontends/shell/package.json ./shell/

RUN --mount=type=cache,id=jonex-pnpm-store,target=/pnpm-store pnpm install

# Source for build — only the 4 packages needed
COPY frontends/shared/shell-sdk/ ./shared/shell-sdk/
COPY frontends/shared/platform-theme/ ./shared/platform-theme/
COPY frontends/shared/i18n-resources/ ./shared/i18n-resources/
COPY frontends/shared/shared-lib/ ./shared/shared-lib/
COPY frontends/shell/ ./shell/
# 工程化基础配置（tsconfig.base 位于 frontends 根；vite.base.config.ts 为后续统一预留）
COPY frontends/tsconfig.base.json ./
COPY frontends/vite.base.config.ts ./

RUN pnpm --filter @jonex/shell build

FROM nginx:1.27-alpine
WORKDIR /usr/share/nginx/html
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone

COPY --from=builder /app/shell/dist ./
COPY deploy/nginx/shell.conf /etc/nginx/conf.d/default.conf
COPY deploy/docker/frontend-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV API_BASE_URL=/api
ENV ENV=production
ENV APP_TITLE=Jonex

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
