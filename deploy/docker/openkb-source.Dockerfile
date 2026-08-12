# ============================================================
# OpenKB 源码自建镜像（Jonex Atomic Capability 容器）
# ============================================================
# 源码位置：Reference/OpenKB/
# 构建上下文：仓库根（docker-compose build context: ..）
#
# 注意：此镜像启动的是 Jonex start_capability.py，不暴露 OpenKB 原生 API。
# start_capability.py 仅提供 /health 和 /invoke 端点。
# OpenKB Web Workbench UI 不在本方案范围内。
# ============================================================

# ── Python 依赖构建阶段 ──
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV DEBIAN_FRONTEND=noninteractive \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_HTTP_TIMEOUT=120
WORKDIR /app

# 只安装 OpenKB 依赖，不安装项目本身（回避 hatch-vcs 版本 + README.md 缺失）
# jonex_core/ 是纯源码模块，不需要独立安装（运行时靠 PYTHONPATH）
COPY Reference/OpenKB/pyproject.toml Reference/OpenKB/uv.lock ./
RUN --mount=type=cache,target=/root/.local/share/uv \
    uv sync --frozen --no-dev --extra web --no-install-project

# [jonex] Jonex 能力框架最小运行时依赖
# 注意：不要 pip install -r requirements.txt（根 requirements.txt 含 pydantic<2，
# 会与 OpenKB 的 openai-agents / litellm 冲突降级）。用专用最小依赖文件。
COPY deploy/docker/openkb-capability-requirements.txt /tmp/
# [jonex] 必须装进 /app/.venv（运行时使用的环境），而非 UV_SYSTEM_PYTHON 的 /usr/local；
# 否则 sqlalchemy/redis 等 jonex_core 依赖在运行时缺失，start_capability 崩溃。
RUN --mount=type=cache,target=/root/.cache/pip \
    uv pip install --python /app/.venv/bin/python -r /tmp/openkb-capability-requirements.txt

# 源码层（无前端产物）
COPY Reference/OpenKB/openkb/ ./openkb/
COPY jonex_core/ ./jonex_core/
COPY deploy/ ./deploy/

# ── 运行阶段 ──
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app \
    PATH=/app/.venv/bin:$PATH

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/openkb ./openkb
COPY --from=builder /app/jonex_core ./jonex_core
COPY --from=builder /app/deploy ./deploy

# 持久化数据目录
RUN mkdir -p /app/data/kb_storage /app/data/inputs

# [jonex] Jonex Atomic Capability 启动配置
ENV CAPABILITY_KIND=atomic \
    CAPABILITY_NAME=openkb \
    SERVICE_PORT=7566 \
    SERVICE_ENDPOINT=http://openkb:7566 \
    OPENKB_KB_ROOT=/app/data/kb_storage

EXPOSE 7566
ENTRYPOINT ["python", "deploy/start_capability.py"]
