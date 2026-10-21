# syntax=docker/dockerfile:1
# ============================================================
# Jonex Platform - 数据治理能力服务 Dockerfile
# 功能：独立部署 data_governance 能力服务
# 说明：本镜像派生自共享基础镜像 Python_Base（jonex/python-base:local），
#       公共前置层（腾讯源替换、apt 公共三件 gcc/libpq-dev/curl、
#       pip 源配置、requirements.txt 安装）均已在 base 中完成，此处
#       仅追加源码/能力代码（本服务不需要 ffmpeg）。
#       与通用 capability.Dockerfile 的差异：不装 ffmpeg，并额外复制
#       knowledge_base 包（权限解析直接复用其 KB/空间授权服务）。
# ============================================================

ARG PYTHON_BASE=jonex/python-base:local
ARG CAPABILITY_NAME=data_governance

FROM ${PYTHON_BASE} AS base

WORKDIR /app

# 复制核心框架
COPY jonex_core/ ./jonex_core/

# 复制能力代码（通过构建参数指定）
ARG CAPABILITY_NAME
COPY capabilities/${CAPABILITY_NAME}/ ./capabilities/${CAPABILITY_NAME}/

# 数据治理额外依赖 knowledge_base 包（血缘来源的能力侧定义）
COPY capabilities/knowledge_base/ ./capabilities/knowledge_base/

# 复制能力启动模板
COPY deploy/start_capability.py .

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 暴露端口
EXPOSE 8000

# 启动能力服务
CMD ["python", "start_capability.py"]
