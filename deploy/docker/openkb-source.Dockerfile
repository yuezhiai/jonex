# ============================================================
# OpenKB 源码自建镜像（Jonex Atomic Capability 容器）
# ============================================================
# 源码位置：Reference/OpenKB/
# 构建上下文：仓库根（docker-compose build context: ..）
#
# 注意：此镜像启动的是 Jonex start_capability.py，不暴露 OpenKB 原生 API。
# start_capability.py 仅提供 /health 和 /invoke 端点。
# OpenKB Web Workbench UI 不在本方案范围内。
#
# 构建方式（方案 B）：与 mcp-server 一致 —— python:3.12-slim + pip install -r
# 依赖锁定文件 Reference/OpenKB/openkb-requirements.txt 由 uv export 生成：
#   cd Reference/OpenKB && uv export --frozen --no-dev --extra web --format requirements-txt --no-hashes
# 不再依赖 ghcr.io/astral-sh/uv 镜像，OpenShift 部署不受镜像仓库限制。
# ============================================================

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    PIP_DEFAULT_TIMEOUT=120
WORKDIR /app

# 只安装 OpenKB 依赖，不安装项目本身（回避 hatch-vcs 版本 + README.md 缺失；
# 过滤 uv export 生成的 "-e ." 行）
# jonex_core/ 是纯源码模块，不需要独立安装（运行时靠 PYTHONPATH）
COPY Reference/OpenKB/openkb-requirements.txt /tmp/
RUN grep -v '^-e ' /tmp/openkb-requirements.txt > /tmp/openkb-deps.txt \
    && pip install --no-cache-dir -r /tmp/openkb-deps.txt

# [jonex] Jonex 能力框架最小运行时依赖
# 注意：不要 pip install -r requirements.txt（根 requirements.txt 含 pydantic<2，
# 会与 OpenKB 的 openai-agents / litellm 冲突降级）。用专用最小依赖文件。
COPY deploy/docker/openkb-capability-requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/openkb-capability-requirements.txt

# 源码层（无前端产物）
COPY Reference/OpenKB/openkb/ ./openkb/
COPY jonex_core/ ./jonex_core/
COPY deploy/ ./deploy/

# 持久化数据目录
RUN mkdir -p /app/data/kb_storage /app/data/inputs

# [jonex] Jonex Atomic Capability 启动配置
ENV PYTHONPATH=/app \
    CAPABILITY_KIND=atomic \
    CAPABILITY_NAME=openkb \
    SERVICE_PORT=7566 \
    SERVICE_ENDPOINT=http://openkb:7566 \
    OPENKB_KB_ROOT=/app/data/kb_storage

EXPOSE 7566
ENTRYPOINT ["python", "deploy/start_capability.py"]
