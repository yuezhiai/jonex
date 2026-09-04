# Jonex 平台部署架构

本文描述当前部署拓扑。系统按全新架构维护：生产环境浏览器只访问 `frontend-gateway`，前端业务请求统一通过 `/api/**` 进入 API Gateway。

## 1. 总体拓扑

```text
用户浏览器
  -> frontend-gateway:80
     -> shell-frontend
     -> core-business-frontend
     -> platform-management-frontend
     -> ecosystem-management-frontend
     -> /api/** -> gateway:8000
        -> sidecar:8001
           -> platform-service:8006
           -> business-domain-service:8005
           -> knowledge-base-service:8003
           -> atomic-rag:8004 -> lightrag:9621

PostgreSQL / Redis / Milvus / etcd / MinIO
```
                ┌──────────────────────────────────────────────────────────┐
                │                客户端 / 第三方平台                         │
                └───────────────────────────┬──────────────────────────────┘
                                            │
                ┌───────────────────────────▼──────────────────────────────┐
                │     frontend-gateway (Nginx, 80) — 唯一对外入口           │
                │   静态资源 / SPA 路由回退 / /api、/ec 反代 / CSP 安全头   │
                └───┬───────────────────────────────────────────┬──────────┘
                    │ 内部反代到 5 个子前端                       │ /api、/ec 反代
                    ▼                                            ▼
   ┌───────────────────────────────────────────────┐    ┌────────────────────┐
   │ shell / core-business /                        │    │  API Gateway       │
   │ 8000)   │
   │ ecosystem-management                          │    │  业务路由聚合       │
   │ (各自独立 Nginx 容器，仅内部 expose:80)        │    │  + 文件落盘        │
   └───────────────────────────────────────────────┘    └─────────┬──────────┘
                                                                  │
                                            ┌─────────────────────▼──────────────────────┐
                                            │     Sidecar 代理 (FastAPI, 容器内 8000)     │
                                            │   认证鉴权 / 调用计量 / 内部 JWT / 反向代理 │
                                            └────┬─────────────┬──────────────┬──────────┘
                                                 │             │              │
                            ┌────────────────────▼─┐  ┌────────▼───────┐  ┌──▼─────────────────┐
                            │ knowledge-base       │  │ business-domain│  │  atomic-rag        │
                            │ (业务能力，8000)     │  │ (业务能力,8000)│  │  (原子能力, 8000)  │
                            │ business.kb          │  │ business.bd    │  │  atomic.rag.lightrag│
                            └──────────────────────┘  └────────────────┘  └────────┬───────────┘
                                                                                   │ HTTP X-API-Key
                                                                                   ▼
                                                                          ┌────────────────────┐
                                                                          │  lightrag (9621)   │
                                                                          │  RAG 引擎 + WebUI  │
                                                                          └────────────────────┘

                ┌──────────────────────────────────────────────────────────┐
                │                       基础设施层                          │
                │  PostgreSQL 15 │ Redis 7 │ Milvus 2.5 │ etcd 3.5 │ MinIO  │
                │     PG 含 platform / knowledge_base / ontology schema │
                └──────────────────────────────────────────────────────────┘
```

## 2. 对外暴露原则

| 模式 | 对外端口 | 说明 |
|---|---|---|
| 生产 | `frontend-gateway:80` 或上层 HTTPS LB | 唯一浏览器入口。后端、能力服务和基础设施仅容器网络访问。 |
| 开发 | `80`、`8000`、`8001`、`8003`、`8004`、`8005`、`8006`、`9621` | 通过 override 暴露，方便本机调试。 |

生产浏览器路径：

| 路径 | 目标 |
|---|---|
| `/` | Shell |
| `/apps/{app-id}/**` | Shell hosted 子应用路由 |
| `/{app-id}/**` | 子应用 standalone 路由 |
| `/remotes/{app-id}/**` | Module Federation remote assets |
| `/api/**` | API Gateway |

## 3. 前端容器

| 容器 | 职责 |
|---|---|
| `frontend-gateway` | 唯一入口，聚合静态资源，反代 `/api/**`，设置安全头和缓存策略。 |
| `shell-frontend` | 壳应用，负责登录后工作台、应用加载、导航和上下文注入。 |
| `core-business-frontend` | 核心业务前端。 |
| `platform-management-frontend` | 平台管理前端。 |
| `ecosystem-management-frontend` | 生态管理前端。 |

应用清单生产来源：

```text
GET /api/v1/platform/frontend/apps
```
浏览器 →[POST 上传文件]→ frontend-gateway →[反代]→ Gateway
   ↓
Gateway 落盘到共享卷 jonex-rag-inputs:/app/inputs，记录元数据
   ↓
Gateway →[/invoke business.knowledge_base.v1]→ Sidecar →[+ 内部 JWT]→ knowledge-base
   ↓
knowledge-base CRUD 状态机 →[/invoke atomic.rag.lightrag.v1 action=insert]→ Sidecar → atomic-rag
   ↓
atomic-rag 异步 worker 解析（mineru/docling/paddleocr）→ ASR（视频/音频走 ffmpeg + whisper）
   ↓
atomic-rag →[POST /documents/text]→ lightrag（生成 track_id）
   ↓
atomic-rag 轮询 GET /documents/track_status/{track_id} 拿 doc_id → 写回任务状态
   ↓
（可选）Stage 4 本体抽取（ONTOLOGY_EXTRACT_ENABLED=true 时执行）
   ├─ 读共享 volume 中 LightRAG 已抽候选实体
   ├─ OntologyExtractor 调 LLM 按 TBox schema 归类/消歧/补属性
   └─ 结果写 Redis task → knowledge-base reconcile 通过 Cypher MERGE 写入 Neo4j（:OntologyEntity / [:ONT_REL]）
   ↓
（可选）atomic-rag → RAG_WEBHOOK_URL 回调 knowledge-base 更新文档状态
```

静态 `frontends/shell/public/app-manifest.json` 只用于本地开发和显式 fallback。

## 4. 后端容器

| 容器 | 端口 | 职责 |
|---|---:|---|
| `gateway` | `8000` | 外部 API 路由聚合。 |
| `sidecar` | `8001` | 认证、租户上下文、内部 JWT、计量、限流、熔断、能力代理。 |
| `knowledge-base` | `8003` | 知识库、文档状态机、RAG 接入。 |
| `atomic-rag` | `8004` | RAG 原子能力。 |
| `business-domain` | `8005` | 领域空间、领域服务、引擎、适配器。 |
| `platform` | `8006` | 登录、RBAC、菜单、应用注册、审计、任务调度。 |
| `lightrag` | `9621` | 索引、检索、生成和 WebUI。 |

后端调用链：

```text
Gateway -> Sidecar -> Capability Service -> Repository -> PostgreSQL / Redis
```

## 5. 基础设施

| 服务 | 用途 |
|---|---|
| `postgres` | 平台和业务主数据。 |
| `redis` | 服务发现、心跳、任务状态、缓存、治理状态。 |
| `milvus` | 向量检索。 |
| `etcd` | Milvus 元数据依赖。 |
| `minio` | Milvus 对象存储依赖。 |

## 6. Nginx 规则

`deploy/nginx/app-locations.conf` 维护外部入口的业务路由规则（监听与 TLS 由
`deploy/nginx/modes/server-{http,https}.conf` 按 `ACCESS_MODE` 二选一，
见 `docs/https-domain-access-plan.md`）：

- `/api/**` 反代到 `gateway:8000`。
- `/remotes/{app-id}/**` 反代到对应子应用容器。
- `/{app-id}/**` 提供子应用 standalone SPA fallback。
- Shell 路由 fallback 到 `shell-frontend`。
- 静态 assets 使用长缓存，HTML 和 manifest 使用短缓存。

每个子应用自身的 `nginx/default.conf` 只服务本应用静态文件、standalone fallback 和 remote assets。

## 7. 数据迁移

PostgreSQL migrations 位于 `deploy/postgres/migrations/`。新增迁移必须遵守：

- 平台共享元数据不带 `tenant_id`，例如应用注册、菜单、权限、系统配置。
- 平台运行数据和业务数据必须带合法 `tenant_id`。
- 本地 seed 使用 `tenant_jonex_demo`。
- 不写入默认业务租户。
- 新业务表默认包含统一实体字段：`tenant_id`、时间戳、软删除字段，必要时包含审计字段。

## 8. 运维命令
### 2.4 业务能力容器（knowledge-base）

由 [`deploy/docker/capability.Dockerfile`](docker/capability.Dockerfile) 构建，`CAPABILITY_NAME` 由构建参数指定，启动时由 `deploy/start_capability.py` 完成：

1. 动态导入 `capabilities/<name>/` 包，注册到本地 `CapabilityRegistry`
2. 调用 `capability.initialize()` 完成数据库连接、缓存预热
3. 注册 `ServiceInstance` 到服务发现中心（Redis），启动 30s 周期心跳
4. 暴露 `/invoke`（带 `Depends(verify_internal_service)`） + `/health`

```yaml
# 单实例资源建议
resources:
  limits:   { cpus: '4', memory: 4G }
  requests: { cpus: '1', memory: 1G }
```

### 2.5 atomic-rag（RAG 原子能力容器）

由 [`deploy/docker/atomic-rag.Dockerfile`](docker/atomic-rag.Dockerfile) 构建，特性：

- 系统依赖：libreoffice / ffmpeg / poppler-utils / tesseract-ocr + chi-sim / git
- 直接打包 `Reference/Rag-anything/` 源码到 `/opt/raganything`，`pip install -e ".[all]"` 安装全部 extras
- 构建期 [`download_models.py`](docker/download_models.py) 预下载 whisper base + MinerU2.5-Pro VLM + PaddleX OCR（约 3-4 GB 烤进镜像）
- 启动入口 `start_capability.py`，加载 `LightRAGAdapter`：
  - 注册 `atomic.rag.lightrag.v1` 到 CapabilityRegistry + 服务发现
  - 等待 lightrag `/health` 就绪后初始化（最多 120s）
  - 启动 `RAG_WORKER_NUM`（默认 2）个 ingest worker 异步消费解析任务
  - 注册 `/query/stream` NDJSON 端点
- 视频/音频处理：`_ingest_worker` 检测扩展名后分支到 `parse_video` / `parse_audio` / `parse_document`，视频/音频用 ffmpeg 提 16kHz 单声道 wav，走 whisper 转写后追加 `[视频转写]` / `[音频转写]` 文本块
- GPU 自动检测：`torch.cuda.is_available()` 时把 `device="cuda"` 透传给 mineru；GPU 叠加 `docker-compose.gpu.yml` 分配 NVIDIA 设备
- 本体抽取（可选）：`ONTOLOGY_EXTRACT_ENABLED=true` 时初始化 `OntologyExtractor`，在文档入库后执行 Stage 4 本体抽取，通过 LLM 按 TBox schema 归类/消歧/补属性，结果写入 Redis task → reconcile 用 Cypher MERGE 写入 Neo4j `:OntologyEntity` / `[:ONT_REL]`
- 健康检查 `start_period: 300s`（mineru 首次启动需要解压模型）

```yaml
deploy:
  resources:
    reservations:
      devices: [{driver: nvidia, count: all, capabilities: [gpu]}]
```

### 2.6 lightrag

由 [`deploy/docker/lightrag-source.Dockerfile`](docker/lightrag-source.Dockerfile) 构建（`jonex-lightrag-source`），基于工程内集成的 LightRAG 源码（`Reference/LightRAG`，lightrag-hku 1.4.16）多阶段自建。源码改造（如 `lightrag/llm/ollama.py` 中 `ollama_client.chat()` 注入 `think` 开关关闭 Qwen3.5 思考模式）直接落在源码中，`git diff` 可审查。

- 仅容器内 `expose:9621`，atomic-rag 通过 `LIGHTRAG_API_URL=http://lightrag:9621` 调用，附 `X-API-Key`
- 配置文件 `.env.rag`（70+ 项，70+ 项与平台主 `.env` 分离），支持 JSON/Nano/PG/Milvus/Qdrant/Neo4j/Mongo/Redis/Memgraph/OpenSearch 多种存储后端
- LLM 走 Ollama 原生协议（`LLM_BINDING=ollama`，`BINDING_HOST` 不含 `/v1` 路径），Embedding 仍走 OpenAI 兼容协议
- 通过 `extra_hosts: host.docker.internal:host-gateway` 在 Linux 下访问宿主 Ollama

### 2.7 基础设施容器

| 服务 | 镜像 | 容器内端口 | 数据持久化 | 备注 |
|------|------|-----------|-----------|------|
| PostgreSQL | `postgres:15`（Debian 版，非 alpine：musl 版在 CentOS 7 内核 3.10 上写 WAL 失败） | 5432 | `jonex-postgres-data` | LTS → 2027-11；含 platform / knowledge_base / ontology 三个 schema |
| Redis | `redis:7-alpine` | 6379 | `jonex-redis-data` | 服务发现注册中心 / 缓存 / 分布式锁；⚠️ 4.0 已 EOL，禁止降级 |
| etcd | `quay.io/coreos/etcd:v3.5.18` | 2379 | `jonex-etcd-data` | Milvus 元数据，硬性要求 ≥3.5.0 |
| MinIO | `minio/minio:RELEASE.2025-04-22T22-12-26Z` | 9000 / 9001 | `jonex-minio-data` | Milvus 对象存储，需 S3 v4 签名 |
| Milvus | `milvusdb/milvus:v2.5.14` | 19530 / 9091 | `jonex-milvus-data` | BM25 + 混合检索（备用，Knowledge Base 当前走 lightrag） |
| Neo4j | `neo4j:5.26-community` | 7687 (bolt) / 7474 (browser) | `jonex-neo4j-data` + `jonex-neo4j-logs` | 本体图数据库（`:OntologyEntity` / `[:ONT_REL]`，APOC 插件）；knowledge-base 通过 `NEO4J_URI` 连接 |

### 2.8 Neo4j 本体图数据库

Neo4j 5.26-community 容器（`deploy/docker-compose.yml`）专用于本体 ABox 存储：

- **镜像**：`neo4j:5.26-community`（APOC 插件已启用，提供 `apoc.coll.toSet`、`apoc.map.merge` 等原子操作）
- **端口**：7687 (Bolt，仅容器内) / 7474 (HTTP Browser，开发期可选暴露)
- **持久化**：`jonex-neo4j-data`（`/data`）+ `jonex-neo4j-logs`（`/logs`）
- **内存**：`NEO4J_HEAP=1G`（可配）、`NEO4J_PAGECACHE=512M`（可配）
- **schema 初始化**：knowledge-base 启动时调用 `ensure_ontology_schema()`，创建复合唯一键约束 `ont_entity_key` + 全文索引 `ont_entity_ft`
- **依赖关系**：knowledge-base + lightrag + atomic-rag 均 `depends_on: neo4j: condition: service_healthy`

### 2.9 本体 schema 配置

`deploy/config/ontology/` 目录存放本体 TBox YAML 定义：

| 文件 | 用途 |
|------|------|
| `default.yaml` | 默认本体 schema（Organization / Person / Product / Concept 等实体类型 + BELONGS_TO / PRODUCES 等关系类型） |

通过 `ONTOLOGY_EXTRACT_ENABLED=true` 启用后，atomic-rag 在文档入库完成后执行 Stage 4 本体抽取。

### 2.9 GPU 加速

`deploy/docker-compose.gpu.yml` 为 atomic-rag 分配 NVIDIA GPU：

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

启用后 `torch.cuda.is_available()` 返回 True，MinerU 和 PyTorch 自动使用 CUDA，atomic-rag CPU 内存从 ~4G 降至 ~2G，显存占用约 6-8G。

## 三、网络设计

### 3.1 网络拓扑

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                       jonex-network (172.28.0.0/16)                          │
│                                                                              │
│  ┌──────────┐ ┌────────┐ ┌──────┐ ┌───────┐ ┌──────────┐ ┌──────────┐        │
│  │postgres  │ │ redis  │ │ etcd │ │ minio │ │  milvus  │ │  neo4j   │  ← 仅内部   │
│  │  5432    │ │ 6379   │ │ 2379 │ │ 9000  │ │  19530   │ │7687/7474 │           │
│  └──────────┘ └────────┘ └──────┘ └───────┘ └──────────┘ └──────────┘        │
│                                                                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ ┌──────────────┐                  │
│  │ gateway  │ │ sidecar  │ │business-dom. │ │knowledge-base│                  │
│  │   8000   │ │   8000   │ │   8000       │ │     8000     │                  │
│  │(开:8000) │ │(开:8001) │ │(开:8005)     │ │(开:8003)     │                  │
│  └──────────┘ └──────────┘ └──────────────┘ └──────────────┘                  │
│                                                                              │
│  ┌────────────┐ ┌──────────┐                                                 │
│  │ atomic-rag │ │ lightrag │                                                 │
│  │   8000     │ │   9621   │                                                 │
│  │ (开:8004)  │ │(开:9621) │                                                 │
│  └────────────┘ └──────────┘                                                 │
│                                                                              │
│  ┌──────────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐           │
│  │ frontend-gateway │ │   shell    │ │ core-      │ │ platform-  │ ...       │
│  │       80         │ │  frontend  │ │ business   │ │ management │           │
│  │   (对外:80)      │ │ (内部:80)  │ │ (内部:80)  │ │ (内部:80)  │           │
│  └──────────────────┘ └────────────┘ └────────────┘ └────────────┘           │
│                                                                              │
│  生产模式 (make up-prod)：仅 frontend-gateway:80 对外                         │
│  开发模式 (make up)：加载 docker-compose.override.yml，额外暴露               │
│           gateway:8000 / sidecar:8001 / atomic-rag:8004                       │
│           knowledge-base:8003 / lightrag:9621                                 │
│  宿主机单服务调试 (make docker-local-up)：加载 debug compose，供               │
│           sidecar 反代宿主机业务后端或 atomic-rag                             │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 安全策略

- **数据库 / Redis / etcd / MinIO / Milvus**：仅内部网络访问，不映射宿主端口
- **能力服务 + atomic-rag**：仅 Sidecar 可调用 `/invoke`，校验内部 JWT（`verify_internal_service`）
- **lightrag**：仅 atomic-rag 可调用，使用 `X-API-Key` 鉴权
- **Sidecar / Gateway**：生产模式仅容器内可访问，由 frontend-gateway 反向代理
- **frontend-gateway**：宿主映射 80，作为生产唯一对外入口；CSP / 安全头由本层控制
- **Kubernetes 部署**：可叠加 NetworkPolicy 进一步限制东西向流量

## 四、数据持久化设计

### 4.1 命名卷一览（顶层 `volumes:` 段）

| 卷名 | 用途 | 挂载到 |
|------|------|--------|
| `jonex-postgres-data` | PG 数据 | `postgres:/var/lib/postgresql/data` |
| `jonex-redis-data` | Redis AOF/RDB | `redis:/data` |
| `jonex-etcd-data` | Milvus 元数据 | `etcd:/etcd` |
| `jonex-minio-data` | Milvus 对象存储 | `minio:/minio_data` |
| `jonex-milvus-data` | Milvus 引擎数据 | `milvus:/var/lib/milvus` |
| `jonex-rag-storage` | RAG 解析缓存（atomic-rag/lightrag 共享） | atomic-rag:`/app/rag_storage`<br>lightrag:`/app/data/rag_storage` |
| `jonex-rag-inputs` | 上传文件原件（gateway/atomic-rag/lightrag 共享） | gateway:`/app/inputs`<br>atomic-rag:`/app/inputs`<br>lightrag:`/app/data/inputs` |
| `jonex-rag-models` | RAG 模型缓存（HF / modelscope / whisper / torch） | atomic-rag:`/root/.cache` |
| `jonex-logs` | 应用日志聚合 | 各服务:`/app/logs` |

### 4.2 模型缓存的两个关键约定

1. **挂载点必须命中默认缓存路径**：raganything / mineru / whisper 进程查找模型走 `HF_HOME` / `HF_HUB_CACHE` / `MODELSCOPE_CACHE` / `TORCH_HOME`，全部位于 `/root/.cache/*` 下。挂卷到父目录 `/root/.cache` 才能让进程真正读到，挂到 `/app/models` 等其他路径会"白挂"。
2. **named volume 首次空卷复制语义**：构建期 `download_models.py` 把模型烤进镜像的 `/root/.cache/*`。首次 `docker compose up`（卷不存在）时镜像内的模型会被复制进卷；之后 `down && up`（卷保留）会以卷为准——**镜像里新版本的模型不会自动覆盖卷**。模型升级流程：
   ```cmd
   docker compose stop atomic-rag
   docker volume rm <project>_jonex-rag-models
   docker compose up -d atomic-rag    # 重新从镜像复制最新模型
   ```

### 4.3 共享卷协作矩阵

| 卷 | gateway | knowledge-base | atomic-rag | lightrag |
|---|---|---|---|---|
| `jonex-rag-inputs` | 写（上传落盘） | 读（元数据） | 读写（解析输入 + 图片资产写入，local 后端） | 读（reference） |
| `jonex-rag-storage` | — | — | 写（解析中间产物） | 读（RAG workspace） |
| `jonex-rag-models` | — | — | 读写（模型缓存） | — |

## 五、服务发现与通信

### 5.1 服务间通信矩阵

| 链路 | 协议 | 端口（容器内） | 鉴权 | 说明 |
|------|------|--------------|------|------|
| 客户端 → frontend-gateway | HTTPS / HTTP | 80 | — | 唯一对外入口；静态资源 + /api、/ec 反代 |
| frontend-gateway → 子前端 | HTTP | 80 | — | nginx 静态资源反代到 5 个子前端 |
| frontend-gateway → Gateway | HTTP/REST | 8000 | — | nginx `proxy_pass http://gateway:8000`；`/ec` 通过 rewrite 改写 |
| Gateway → Sidecar | HTTP/REST | 8000 | — | 业务路由 → `/invoke`；流式搜索 → `/invoke/stream/rag` |
| Sidecar → knowledge-base | HTTP/REST | 8000 | 内部 JWT | `business.knowledge_base.v1` |
| Sidecar → atomic-rag | HTTP/REST | 8000 | 内部 JWT | `atomic.rag.lightrag.v1`；流式查询直连 `/query/stream` |
| atomic-rag → lightrag | HTTP/REST | 9621 | `X-API-Key` | LightRAG 官方 REST API |
| 能力服务 → PostgreSQL | TCP | 5432 | 用户名/密码 | 数据存取 |
| 能力服务 → Redis | TCP | 6379 | — | 缓存 / 服务发现 / 分布式锁 |
| knowledge-base → Neo4j | Bolt | 7687 | 用户名/密码 | 本体图数据 CRUD（`:OntologyEntity` / `[:ONT_REL]`） |
| lightrag → Neo4j | Bolt | 7687 | 用户名/密码 | LightRAG `Neo4JStorage` 图后端（Label 隔离） |

### 5.2 服务发现机制

**Docker Compose 环境**：
- 容器互通走 Docker 内置 DNS（服务名即 hostname：`postgres`、`redis`、`knowledge-base-service`、`atomic-rag` 等）
- 能力服务启动时通过 `start_capability.py` 注册 `ServiceInstance` 到 Redis（key: `jonex:service:<service_name>`），30s 周期心跳
- Sidecar 调用时优先 `service_registry.discover(service_name)`，失败回退静态配置 `_static_endpoints`

**Kubernetes 环境**：
- Service + CoreDNS 解析；StatefulSet 用 Headless Service

### 5.3 能力 ID 命名规范

```
{kind}.{name}.v{major}
```

| 类型 | 示例 | 启动方式 |
|------|------|---------|
| `business` | `business.knowledge_base.v1`、`business.business_domain.v1` | `capabilities/<name>/` 包，类名 PascalCase + `Capability` 后缀 |
| `domain` | `domain.rag.text.v1` | `jonex_core/capability/domain/<name>/` |
| `atomic` | `atomic.rag.lightrag.v1`、`atomic.audio.whisper.v1`（规划） | `jonex_core/capability/atomic/<name>/` |

`start_capability.py` 通过 `module_overrides` 把 `(atomic, "rag.lightrag")` 映射到 `LightRAGAdapter`、`(domain, "rag.text")` 映射到 `DomainRAGText`，其他自动按命名约定加载。

### 5.4 内部 JWT 认证

- Sidecar 调用任何能力服务 `/invoke` 时，`jonex_core/sidecar/proxy.py` 调用 `auth.generate_token("sidecar")` 签发短时（5 min）JWT
- 能力服务 `/invoke` 端点用 `Depends(verify_internal_service)` 校验 `Authorization: Bearer <token>`
- 双方共享 `JWT_SECRET`（来自 `.env`）+ `JWT_ALGORITHM`（默认 HS256）
- atomic-rag 的 `/query/stream` 端点目前**未启用**该校验（由 sidecar 网络隔离兜底；如需统一可在 `LightRAGAdapter.register_routes` 加 Depends）

## 六、健康检查设计

| 服务 | 健康端点 | start_period | 备注 |
|------|---------|-------------|------|
| postgres | `pg_isready` | 默认 | — |
| redis | `redis-cli ping` | 默认 | — |
| etcd | `etcdctl endpoint health` | 默认 | — |
| minio | `/minio/health/live` | 默认 | — |
| milvus | `/healthz` | 90s | 启动较慢 |
| gateway / sidecar / knowledge-base / business-domain / platform | `GET /health` | 30s | 标准能力服务模板 |
| atomic-rag | `GET /health` | **300s** | mineru 首次启动需解压模型 |
| lightrag | `GET /health` | 60s | LightRAG 引擎冷启动 |
| frontend-gateway / 5 个子前端 | `GET /health` | 5-10s | nginx 自身 |

## 七、扩缩容策略

### 7.1 水平扩展

```bash
make build
make up
make ps
make logs
make down
```

单服务：

```bash
make rebuild-service SERVICE=platform-service
make restart-service SERVICE=platform-service
make logs-service SERVICE=platform-service
```

前端：

```bash
make rebuild-frontend-gateway
make rebuild-shell-frontend
make rebuild-core-business-frontend
make rebuild-platform-management-frontend
make rebuild-ecosystem-management-frontend
```

## 9. 规范入口

- 系统架构：[../jonex-platform-architecture.md](../jonex-platform-architecture.md)
- 后端规范：[../backend-development-standard.md](../backend-development-standard.md)
- 前端规范：[../frontend-development-standard.md](../frontend-development-standard.md)
