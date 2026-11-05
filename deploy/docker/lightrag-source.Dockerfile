# ============================================================
# LightRAG 源码自建镜像（集成到工程，替代官方镜像 + sed patch）
# ============================================================
# 源码位置：Reference/LightRAG/（已剔除 .git / .venv / data / tests / examples）
# 构建上下文：仓库根（docker-compose build context: ..）
#
# 与官方 Dockerfile 的差异：
#   - 不再 FROM ghcr.io/hkuds/lightrag:latest，改为源码 pip/uv 构建
#   - 源码改造（think 开关、ENTITY_TYPES、/custom-kg、结构化查询）直接落在
#     Reference/LightRAG/lightrag/ 源码里，git diff 可审查，不靠运行期替换
#   - 固定版本：lightrag-hku 1.4.16 (api 0292)
# ============================================================

# [jonex] syntax=docker/dockerfile:1 已注释 — Docker Hub 不可达，Docker 29.x 内置 BuildKit 已支持所需特性
# syntax=docker/dockerfile:1

# ── 前端构建阶段（WebUI）──
FROM --platform=$BUILDPLATFORM oven/bun:1.3.14-alpine AS frontend-builder
# 构建源镜像开关：false=国内源（npmmirror），true=国外官方源（registry.npmjs.org）
ARG USE_OVERSEAS_MIRROR=false
WORKDIR /app
# [jonex] bun 默认走 registry.npmjs.org 国内不稳（mermaid tarball 提取失败），切 npmmirror 源；
# 国外源构建时保持官方 registry
RUN if [ "$USE_OVERSEAS_MIRROR" = "true" ]; then \
        printf '[install]\nregistry = "https://registry.npmjs.org"\n' > /root/.bunfig.toml; \
    else \
        printf '[install]\nregistry = "https://registry.npmmirror.com"\n' > /root/.bunfig.toml; \
    fi
COPY Reference/LightRAG/lightrag_webui/ ./lightrag_webui/
RUN --mount=type=cache,target=/root/.bun/install/cache \
    cd lightrag_webui \
    && bun install --frozen-lockfile \
    && bun --bun ./node_modules/vite/bin/vite.js build

# ── Python 构建阶段（uv 锁定依赖）──
# 注意：所有依赖均以预编译 wheel 形式下载，无需 build-essential / Rust！
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
# 构建源镜像开关：false=国内源（清华 pypi），true=国外官方源（pypi.org）
ARG USE_OVERSEAS_MIRROR=false
ENV DEBIAN_FRONTEND=noninteractive \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_HTTP_TIMEOUT=120
WORKDIR /app

# uv 源：国内=清华镜像；国外=官方 PyPI（uv.toml 对后续全部 uv sync 生效）
RUN if [ "$USE_OVERSEAS_MIRROR" = "true" ]; then \
        mkdir -p /root/.config/uv && printf 'index-url = "https://pypi.org/simple"\n' > /root/.config/uv/uv.toml; \
    else \
        mkdir -p /root/.config/uv && printf 'index-url = "https://pypi.tuna.tsinghua.edu.cn/simple"\n' > /root/.config/uv/uv.toml; \
    fi

# 依赖元数据先行（利用层缓存）
COPY Reference/LightRAG/pyproject.toml .
COPY Reference/LightRAG/setup.py .
COPY Reference/LightRAG/uv.lock .
RUN --mount=type=cache,target=/root/.local/share/uv \
    uv sync --frozen --no-dev --extra api --extra offline --no-install-project

# 源码层（含工程对 LightRAG 的改造）
COPY Reference/LightRAG/lightrag/ ./lightrag/
COPY --from=frontend-builder /app/lightrag/api/webui ./lightrag/api/webui
RUN --mount=type=cache,target=/root/.local/share/uv \
    uv sync --frozen --no-dev --extra api --extra offline

# tiktoken 离线词表——用工程内 vendored 文件，构建期不联网。
#
# 原写法是构建期下载 + timeout 90s 兜底，2026-09-16 实测其失效：lightrag-download-cache
# 默认下 8 个模型，实测单个就要 68~90s，timeout 在 [1/8] 即掐断，而 `|| true` 让构建
# 照常报成功，产物镜像里 /app/data/tiktoken 是**空目录**。后果是静默的：上一版镜像里
# 那两份表就此丢失，之后每次容器重建都在运行期重下（实测启动多花 21s）。故弃用下载。
#
# 只带 o200k_base（文件 fb374d41...）：lightrag 全线默认 gpt-4o-mini
# （lightrag.py:326 / rerank.py:26 / ollama_api.py:162），且 api/config.py 无环境变量
# 可改该模型，不存在需要 cl100k_base 的代码路径。与 atomic-rag 共用同一份文件。
#
# 文件名 = 源 URL 的 sha1（tiktoken 的缓存查找键，不可改）；内容 sha256 = tiktoken
# 内置 expected_hash，下方构建期即校验，损坏则拒绝出镜像。详见
# deploy/resources/tiktoken/README.md。
COPY deploy/resources/tiktoken/ /app/data/tiktoken/
RUN echo "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d  /app/data/tiktoken/fb374d419588a4632f3f557e76b4b70aebbca790" \
      | sha256sum -c - \
    || { echo "[ERROR] vendored tiktoken 词表缺失或损坏（sha256 校验失败）。" >&2; \
         echo "[ERROR] 请参照 deploy/resources/tiktoken/README.md 重新获取后重试。" >&2; \
         exit 1; }

# ── 运行阶段 ──
# .venv 已从 builder 完整复制，无需再次 uv sync
FROM python:3.12-slim
# ARG 不跨 stage 继承：最终镜像的 label 要在本 stage 重新声明才能取到值
ARG USE_OVERSEAS_MIRROR=false
LABEL org.jonex.build.mirror="${USE_OVERSEAS_MIRROR}"
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/lightrag ./lightrag
COPY --from=builder /app/data/tiktoken /app/data/tiktoken

RUN mkdir -p /app/data/rag_storage /app/data/inputs

ENV TIKTOKEN_CACHE_DIR=/app/data/tiktoken \
    WORKING_DIR=/app/data/rag_storage \
    INPUT_DIR=/app/data/inputs

EXPOSE 9621
ENTRYPOINT ["python", "-m", "lightrag.api.lightrag_server"]
