# ============================================================
# atomic-rag 原子能力容器（本地 raganything 源码打包）
# ============================================================
# 依赖胖瘦由 build arg RAG_PROFILE 控制（full 默认 / slim）：
#   full ：含本地 mineru CLI + 音视频 ASR，RAG_PARSER=mineru/mineru_online/mineru_selfhost 皆可真跑
#   slim ：online/selfhost 纯 HTTP 客户端，去 torch/CUDA/mineru（省 ~3.9GB），本地 mineru 不可用
# 打包切换：`RAG_PROFILE=slim docker compose build atomic-rag`（compose 已透传该环境变量）
#
# 直接打包 Reference/Rag-anything 中的 raganything 源码
# full 画像包含完整的多模态文档解析能力：
# - libreoffice（Word/PPT/Excel 解析）
# - ffmpeg（音频/视频处理，含视频关键帧提取）
# - poppler-utils（PDF 渲染）
# - tesseract-ocr + tesseract-ocr-chi-sim（OCR 文字识别）
# - whisper base 模型（ASR 语音转写，构建时预下载）
# - mineru V3 + VLM / layout / OCR 模型（构建时预下载约 2-4GB）
# - mineru / docling / paddleocr 解析器

FROM python:3.12.13-slim AS base

WORKDIR /app

# 构建源镜像开关：false=国内源（腾讯云 apt/pip），true=国外官方源（deb.debian.org / pypi.org）
ARG USE_OVERSEAS_MIRROR=false

# 时区
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo "Asia/Shanghai" > /etc/timezone

# 替换腾讯源；国外源构建时跳过。
# trixie-updates 组件腾讯云镜像未同步（无 Release 文件），apt update 会失败，故从 Suites 中移除。
RUN if [ "$USE_OVERSEAS_MIRROR" != "true" ]; then \
        sed -i \
            -e 's|^URIs: http://deb.debian.org/debian$|URIs: http://mirrors.cloud.tencent.com/debian|' \
            -e 's|^URIs: http://deb.debian.org/debian-security$|URIs: http://mirrors.cloud.tencent.com/debian-security|' \
            -e 's| trixie-updates||g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi

# ── 第 1 层：系统依赖（apt 缓存复用）──
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    ffmpeg \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    fonts-wqy-microhei \
    wget \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# pip 源配置：国内=腾讯云镜像；国外=官方 PyPI
RUN if [ "$USE_OVERSEAS_MIRROR" = "true" ]; then \
        pip config set global.index-url https://pypi.org/simple; \
    else \
        pip config set global.index-url https://mirrors.cloud.tencent.com/pypi/simple \
        && pip config set global.trusted-host mirrors.cloud.tencent.com; \
    fi
    
# 大包（torch / CUDA wheel / scipy 等）易因镜像源读超时断流：
# 把超时与重试写进 pip config，覆盖下面全部 4 个 pip install 层，
# 配合 --mount=type=cache 让重建续传已下成功的 wheel。
RUN pip config set global.timeout 300 \
    && pip config set global.retries 10

# ── Build args（docker-compose 传参） ──
ARG MINERU_SOURCE=modelscope
# RAG_PROFILE：控制 raganything 依赖胖瘦（代码始终支持三种 RAG_PARSER，本变量只决定装不装重依赖）
#   slim（默认）：装 [image,text]，去掉 mineru/whisper/torch/CUDA（省 ~3.9GB），仅 online/selfhost 可用
#   full        ：装 [all,local]，含本地 mineru CLI + 音视频/paddleocr，三种 parser 都能真跑
# ⚠️ 默认 slim：本地 mineru（RAG_PARSER=mineru）、GPU 加速、音视频解析都需要 full，
#    构建入口 `make build-rag-full`（compose 侧默认值见 deploy/docker-compose.yml）。
ARG RAG_PROFILE=slim

# ── 溯源标签：三个构建期开关全部写进镜像元数据 ──
#   docker inspect <镜像> --format '{{json .Config.Labels}}'
# 目的：同一个 commit 因 RAG_PROFILE 不同会长出 3.9GB 差异与不同的 parser 能力，
# 出问题时先看 label 就知道手里这个镜像到底是怎么打出来的。
LABEL org.jonex.build.mirror="${USE_OVERSEAS_MIRROR}" \
      org.jonex.build.rag_profile="${RAG_PROFILE}" \
      org.jonex.build.mineru_source="${MINERU_SOURCE}"

# ── 环境变量（模型缓存路径 + 能力身份 + Python 路径）──
ENV HF_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/root/.cache/huggingface \
    HF_HUB_CACHE=/root/.cache/huggingface \
    MODELSCOPE_CACHE=/root/.cache/modelscope \
    TORCH_HOME=/root/.cache/torch \
    PYTHONPATH=/app \
    CAPABILITY_NAME=rag.lightrag \
    CAPABILITY_KIND=atomic \
    MINERU_SOURCE=${MINERU_SOURCE} \
    MINERU_MODEL_SOURCE=${MINERU_SOURCE}

# ── 第 2 层：raganything 安装（按 RAG_PROFILE 决定依赖胖瘦；源码隔离到 /opt，避免污染 /app）──
#   full : [all,local] —— 含本地 mineru CLI + 音视频/paddleocr 等全部可选依赖
#   slim : [image,text] —— online/selfhost 纯 HTTP 客户端，不装 mineru/whisper，
#          连带去掉 torch + CUDA(nvidia) + modelscope 等（~3.9GB+）。
#          ⚠️ slim 镜像下 RAG_PARSER=mineru（本地）会因缺依赖在运行时报错，属预期取舍。
COPY Reference/Rag-anything/ /opt/raganything/

RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$RAG_PROFILE" = "slim" ]; then \
      echo "[RAG_PROFILE=slim] 安装精简依赖（online/selfhost，无 torch/mineru）"; \
      pip install -e "/opt/raganything[image,text]"; \
    else \
      echo "[RAG_PROFILE=full] 安装完整依赖（含本地 mineru CLI 与音视频 ASR）"; \
      pip install -e "/opt/raganything[all,local]"; \
    fi

# ── 第 3 层：平台基础依赖（sqlalchemy / redis / pydantic 等）──
COPY requirements.txt /tmp/requirements-base.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements-base.txt

# ── 第 4 层：atomic-rag 专用依赖（httpx / pymupdf / sentence-transformers）──
COPY deploy/docker/atomic-rag-requirements.txt /tmp/requirements-rag.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements-rag.txt

# ── 第 5 层：Pydantic v2 升级（mineru 需要 Pydantic v2 的 computed_field；──
#    config.py try/except 已兼容 v1/v2，属 atomic-rag 镜像的既有运行时例外）──
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install 'pydantic>=2.0,<3.0' pydantic-settings

# ── 第 6 层：模型预下载（--mount=type=cache 确保跨构建复用，不反复下载）──
ARG SKIP_MODEL_DOWNLOAD=true
COPY deploy/docker/download_models.py /tmp/download_models.py
RUN --mount=type=cache,target=/root/.cache/whisper \
    --mount=type=cache,target=/root/.cache/huggingface \
    --mount=type=cache,target=/root/.cache/modelscope \
    --mount=type=cache,target=/root/.cache/torch \
    if [ "$SKIP_MODEL_DOWNLOAD" != "true" ]; then \
      python /tmp/download_models.py && rm /tmp/download_models.py; \
    else \
      echo "[SKIP] 模型预下载跳过"; \
      rm -f /tmp/download_models.py; \
    fi

# ── 第 6.5 层：tiktoken 离线词表（工程内 vendored，构建期不联网）──
#   用途：stages.py 的 PushChunksStage 推送前用 tiktoken 实测每个 chunk 的 token 数，
#   判断是否超过 LightRAG 的 CHUNK_SIZE(1200)——超限会被二次切分，切出的第二片仍带
#   整块的 file_source 元数据，导致表头/行号/页码锚点错位。
#   不内置时 tiktoken 退回默认路径 /tmp/data-gym-cache，而 /tmp 随容器重建清空，
#   于是每次启动都重下 3.6MB 的 o200k_base。实测该下载 17~90s（网络波动极大），
#   占 atomic-rag 启动耗时 94%（2026-09-16 实测 71.6s / 总 76s）。
#   故改为 vendored：文件名 = 源 URL 的 sha1（tiktoken 的查找键，不可改），
#   内容 sha256 = tiktoken 内置 expected_hash，下方构建期即校验，损坏则拒绝出镜像。
#   获取/更新方法见 deploy/resources/tiktoken/README.md。
COPY deploy/resources/tiktoken/ /app/data/tiktoken/
RUN echo "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d  /app/data/tiktoken/fb374d419588a4632f3f557e76b4b70aebbca790" \
      | sha256sum -c - \
    || { echo "[ERROR] vendored tiktoken 词表缺失或损坏（sha256 校验失败）。" >&2; \
         echo "[ERROR] 请参照 deploy/resources/tiktoken/README.md 重新获取后重试。" >&2; \
         exit 1; }

# 显式指向镜像内路径（不依赖 tiktoken 的 /tmp 默认值）
ENV TIKTOKEN_CACHE_DIR=/app/data/tiktoken

# ── 第 7 层前置校验：构建上下文缺失平台源码则终止构建（不产镜像）──
#    BuildKit 无法在 COPY 前直接检查宿主上下文，故以 type=bind 将构建上下文
#    只读挂载到 /build-context，校验关键源码文件存在；缺失则输出指示缺失文件的
#    错误并以非零退出码终止（满足需求 4.7）。该步骤仅做校验，对运行时镜像无任何产物影响。
RUN --mount=type=bind,target=/build-context \
    missing=""; \
    if [ ! -f /build-context/jonex_core/__init__.py ]; then \
      missing="${missing} jonex_core/__init__.py"; \
    fi; \
    if [ ! -f /build-context/deploy/start_capability.py ]; then \
      missing="${missing} deploy/start_capability.py"; \
    fi; \
    if [ -n "${missing}" ]; then \
      echo "[ERROR] 构建上下文缺失平台源码文件，终止构建：${missing}" >&2; \
      echo "[ERROR] 请在仓库根目录（构建上下文）下确认上述文件存在后重试。" >&2; \
      exit 1; \
    fi; \
    echo "[OK] 平台源码校验通过：jonex_core/、deploy/start_capability.py"

# ── 第 7 层：平台源码（变动最频繁，固定为最后一个 COPY 以最大化前 6 层缓存命中）──
RUN mkdir -p /app/output /app/rag_storage
COPY jonex_core/ /app/jonex_core/
COPY deploy/start_capability.py /app/

EXPOSE 8000

# 默认启动 FastAPI 服务（start_capability.py 加载 LightRAGAdapter，注册到 CapabilityRegistry + 服务发现）
CMD ["python", "start_capability.py"]
