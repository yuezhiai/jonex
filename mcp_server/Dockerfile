FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 系统依赖：curl（healthcheck）+ gcc + libpq-dev（asyncpg 编译，安装后会清理）
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
