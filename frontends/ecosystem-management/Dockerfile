# syntax=docker/dockerfile:1
FROM node:20.18-alpine AS builder
WORKDIR /app

RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone
RUN npm config set registry https://registry.npmmirror.com && \
    npm config set fund false && npm config set audit false
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
COPY frontends/ecosystem-management/package.json ./ecosystem-management/

RUN --mount=type=cache,id=jonex-pnpm-store,target=/pnpm-store pnpm install

# Source for build — only the 4 packages needed
COPY frontends/shared/shell-sdk/ ./shared/shell-sdk/
COPY frontends/shared/platform-theme/ ./shared/platform-theme/
COPY frontends/shared/i18n-resources/ ./shared/i18n-resources/
COPY frontends/shared/shared-lib/ ./shared/shared-lib/
COPY frontends/ecosystem-management/ ./ecosystem-management/
# 工程化基础配置（tsconfig.base / vite.base 位于 frontends 根）
COPY frontends/tsconfig.base.json ./
COPY frontends/vite.base.config.ts ./

RUN pnpm --filter @jonex/ecosystem-management build

FROM nginx:1.27-alpine
WORKDIR /usr/share/nginx/html
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone

COPY --from=builder /app/ecosystem-management/dist ./ecosystem-management/
COPY frontends/ecosystem-management/nginx/default.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://127.0.0.1/health || exit 1
CMD ["nginx", "-g", "daemon off;"]
