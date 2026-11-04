FROM python:3.12-slim

# 构建源镜像开关：false=国内源（腾讯云 apt/pip），true=国外官方源（deb.debian.org / pypi.org）
# 与其它镜像同口径，`make build-overseas` 对 mcp-server 也生效。
ARG USE_OVERSEAS_MIRROR=false
# 溯源标签（口径同 python-base.Dockerfile）
LABEL org.jonex.build.mirror="${USE_OVERSEAS_MIRROR}"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120

# 系统依赖：curl（healthcheck）+ gcc + libpq-dev（asyncpg 编译，安装后会清理）
# 源切换：国内档把 debian 源换成腾讯云（trixie-updates 组件腾讯云镜像未同步、无 Release 文件，
# apt update 会失败，故从 Suites 中移除）；pip 源写进 /etc/pip.conf，与 python-base 保持一致。
RUN if [ "$USE_OVERSEAS_MIRROR" != "true" ]; then \
        sed -i \
            -e 's|^URIs: http://deb.debian.org/debian$|URIs: http://mirrors.cloud.tencent.com/debian|' \
            -e 's|^URIs: http://deb.debian.org/debian-security$|URIs: http://mirrors.cloud.tencent.com/debian-security|' \
            -e 's| trixie-updates||g' \
            /etc/apt/sources.list.d/debian.sources; \
        printf '[global]\nindex-url = https://mirrors.cloud.tencent.com/pypi/simple\ntrusted-host = mirrors.cloud.tencent.com\n' > /etc/pip.conf; \
    else \
        printf '[global]\nindex-url = https://pypi.org/simple\n' > /etc/pip.conf; \
    fi
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖定义
COPY mcp_server/requirements.txt ./

# pip 安装
RUN pip install --no-cache-dir -r requirements.txt

# 清理编译依赖（减小镜像体积 + 减少攻击面）
RUN apt-get purge -y gcc libpq-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN groupadd -r mcp && useradd -r -g mcp -m mcp

# 复制源码（排除 Dockerfile，glob 匹配避免逐文件列出）
COPY mcp_server/*.py ./

# 应用目录所有权
RUN chown -R mcp:mcp /app

# 切换到非 root 用户
USER mcp

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8002/health || exit 1

EXPOSE 8002

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
