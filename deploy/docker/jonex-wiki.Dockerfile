# ============================================================
# Jonex Wiki 单容器（B1）：引擎 + Jonex Atomic Capability 同进程 pydantic v2 共存
# ============================================================
# 构建上下文：仓库根（docker-compose build context: ..）
# 引擎源码：Reference/JonexWiki/src；能力框架：jonex_core/
#
# B1 说明：引擎 jonex_wiki 与 JonexWikiCapability 在同一 pydantic v2 进程内运行，
# 适配器进程内 import jonex_wiki 直接调 WikiApplication，不再有独立 adapter 容器 / HTTP 转调。
# 启动的是 Jonex start_capability.py（/invoke + /health + 服务注册），引擎随能力 initialize() 启动。
# ============================================================

FROM python:3.12-slim

# 构建源镜像开关：false=国内源（腾讯云 pip），true=国外官方源（pypi.org）
ARG USE_OVERSEAS_MIRROR=false
# 溯源标签（口径同 python-base.Dockerfile）
LABEL org.jonex.build.mirror="${USE_OVERSEAS_MIRROR}"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120
WORKDIR /app

# pip 源：国内=腾讯云镜像；国外=官方 PyPI
RUN if [ "$USE_OVERSEAS_MIRROR" = "true" ]; then \
        pip config set global.index-url https://pypi.org/simple; \
    else \
        pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple \
        && pip config set global.trusted-host mirrors.cloud.tencent.com; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 引擎依赖（pydantic v2 生态；含 alembic 迁移 src/jonex_wiki/storage/migrations）
COPY Reference/JonexWiki/pyproject.toml ./pyproject.toml
COPY Reference/JonexWiki/alembic.ini ./alembic.ini
COPY Reference/JonexWiki/src ./src
RUN pip install --no-cache-dir .

# Jonex 能力框架最小依赖（不 pin pydantic/fastapi，与引擎 pydantic v2 共存）
COPY deploy/docker/jonex-wiki-capability-requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/jonex-wiki-capability-requirements.txt

# jonex_core 纯源码 + deploy(start_capability.py)
COPY jonex_core/ ./jonex_core/
# 只搬运行时真正需要的：启动脚本 + 本体 tbox 兜底配置（ONTOLOGY_SCHEMA_FALLBACK_PATH）。
# 不再 `COPY deploy/ ./deploy/` —— 那会把 deploy/env/**（各画像的 DB 密码/JWT_SECRET/云凭据）
# 与各种 compose/env 文件一并烤进镜像层。
COPY deploy/start_capability.py ./deploy/
COPY deploy/config/ ./deploy/config/

# [jonex] Jonex Atomic Capability 启动配置（Sidecar 经 JONEX_WIKI_URL 指向本容器 8007）
ENV PYTHONPATH=/app \
    CAPABILITY_KIND=atomic \
    CAPABILITY_NAME=jonex_wiki \
    SERVICE_PORT=8007 \
    SERVICE_ENDPOINT=http://jonex-wiki:8007

EXPOSE 8007
CMD ["sh", "-c", "alembic upgrade head && python deploy/start_capability.py"]
