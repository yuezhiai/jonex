# 身份同步侧车 —— 零第三方依赖（纯 stdlib），镜像只多一个 py 文件
# 构建上下文 = 本目录：deploy/identity-sync-gateway/
FROM python:3.12-slim

# 本镜像零第三方依赖、无构建期联网，ARG 只为与其它镜像的打标口径一致
# （`docker inspect --format '{{json .Config.Labels}}'` 能统一查到构建配置）。
ARG USE_OVERSEAS_MIRROR=false
LABEL org.jonex.build.mirror="${USE_OVERSEAS_MIRROR}"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 非 root 运行：本服务只读不写，无需任何目录权限
RUN useradd --system --uid 10001 --no-create-home gateway

COPY gateway.py /app/gateway.py

USER gateway

EXPOSE 8099

# healthcheck 用 python 而非 curl：基础镜像里没有 curl
# ⚠️ Dockerfile 的 HEALTHCHECK 选项用**连字符**（--start-period），下划线会被 buildkit
#    当成「unknown flag」直接解析失败；compose 里那份才是下划线（start_period），别混。
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health',timeout=5).status==200 else 1)"]

CMD ["python", "/app/gateway.py"]
