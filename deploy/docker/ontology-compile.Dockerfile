# ============================================================
# ontology-compile 单容器（B1）：能力 + worker 同进程
# ============================================================
# 构建上下文：仓库根。无引擎源码、无 GPU —— 只装 jonex_core + 平台依赖。
FROM python:3.12-slim
ARG USE_OVERSEAS_MIRROR=false
# 溯源标签（口径同 python-base.Dockerfile）
LABEL org.jonex.build.mirror="${USE_OVERSEAS_MIRROR}"
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DEFAULT_TIMEOUT=120
WORKDIR /app

RUN if [ "$USE_OVERSEAS_MIRROR" = "true" ]; then \
        pip config set global.index-url https://pypi.org/simple; \
    else \
        pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple \
        && pip config set global.trusted-host mirrors.cloud.tencent.com; \
    fi

# 能力框架最小依赖（两段式：先装基础依赖，再装能力依赖）
COPY deploy/docker/ontology-compile-capability-requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/ontology-compile-capability-requirements.txt

# jonex_core 纯源码 + deploy（start_capability.py 与被 jonex_core 引用的配置）
COPY jonex_core/ ./jonex_core/
# 只搬运行时真正需要的：启动脚本 + 本体 tbox 兜底配置（ONTOLOGY_SCHEMA_FALLBACK_PATH）。
# 不再 `COPY deploy/ ./deploy/` —— 那会把 deploy/env/**（各画像的 DB 密码/JWT_SECRET/云凭据）
# 与各种 compose/env 文件一并烤进镜像层。
COPY deploy/start_capability.py ./deploy/
COPY deploy/config/ ./deploy/config/

# ⚠ SERVICE_ENDPOINT 必须显式写（默认会派生 `atomic-ontology_compile`，与服务发现打架）
# ⚠ 8009 **实测**在 deploy/docker-compose.yml 里零命中（且本服务只 `expose` 不 `ports`，
#   不碰宿主机端口）⇒ 容器网络内无冲突
ENV PYTHONPATH=/app \
    CAPABILITY_KIND=atomic \
    CAPABILITY_NAME=ontology_compile \
    SERVICE_PORT=8009 \
    SERVICE_ENDPOINT=http://ontology-compile:8009
EXPOSE 8009
CMD ["python", "deploy/start_capability.py"]
