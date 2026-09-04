# Jonex 平台系统架构设计文档

> 更新时间：2026-07-15
> 版本：0.5.0
> 文档定位：稳定架构说明。项目议题、风险和后续演进记录见 [PROJECT_ISSUES_AND_TODO.md](PROJECT_ISSUES_AND_TODO.md)。

---

## 一、项目定位

Jonex 平台是一个插件化、多租户的 AI 能力平台。平台由前端工作台、统一 API 入口、Sidecar 能力治理层、多个 capability service、RAG 原子能力和基础设施组件组成。

平台目标不是单个业务应用，而是支撑新子应用、新业务域和新 AI 能力持续接入的统一工程底座。

### 1.1 核心目标

| 目标 | 说明 |
|---|---|
| 多子应用统一入口 | 通过 `frontend-gateway` 聚合 shell、核心业务、平台管理、生态管理 4 个前端应用。 |
| 能力服务独立演进 | `platform`、`business_domain`、`knowledge_base`、`atomic-rag` 可独立部署和扩展。 |
| 统一工程规范 | 后端遵循 [backend-development-standard.md](backend-development-standard.md)，前端遵循 [frontend-development-standard.md](frontend-development-standard.md)。 |
| 显式租户隔离 | 业务数据必须使用合法 `tenant_id`，禁止默认租户兜底。 |
| 薄入口、厚服务 | Gateway 只做 HTTP 路由聚合；Sidecar 做认证、治理和代理；业务规则进入 capability service。 |
| 可治理能力体系 | 能力按原子能力、领域能力、业务能力分层，并通过统一契约调用。 |

### 1.2 技术栈

| 层级 | 技术 |
|---|---|
| 前端 | pnpm workspace、React、TypeScript、Vite、Ant Design、共享主题包、shell-sdk |
| 前端入口 | Nginx `frontend-gateway`，聚合静态资源并反代 `/api/**` |
| 后端框架 | Python 3.12、FastAPI、Uvicorn、Pydantic v1 兼容模式 |
| 数据访问 | SQLAlchemy 2.0 async、asyncpg、统一 `BaseRepository` |
| 数据库 | PostgreSQL 15，schema 包含 `platform`、`business_domain`、`knowledge_base` |
| 缓存和发现 | Redis 7，服务注册、心跳、任务状态和缓存 |
| RAG 和向量 | atomic-rag、LightRAG、Milvus 2.5、etcd 3.5、MinIO |
| 对象存储 | COS（腾讯云对象存储，生产） / 本地文件系统（开发回退），通过 `ObjectStorage` 抽象统一访问 |
| 本体图谱 | TBox YAML、Stage4 本体抽取、Neo4j ABox 图存储、增强搜索 |
| 部署 | Docker、Docker Compose、开发模式多端口暴露、生产模式仅 80 对外 |

---

## 二、总体架构

### 2.1 系统视图

```text
用户浏览器
  |
  v
frontend-gateway (Nginx, 80)
  |-- shell / core-business / platform-management / ecosystem-management
  |-- /api/** -> API Gateway (8000)
                 |
                 v
              Sidecar (8001)
                 |-- 认证 / 租户上下文 / 内部 JWT
                 |-- 计量 / 限流 / 熔断 hook
                 |-- 能力服务代理
                 |
                 +--> platform-service (8006)
                 +--> business-domain-service (8005)
                 +--> knowledge-base-service (8003)
                 +--> atomic-rag (8004) -> lightrag (9621)
                                            -> llm-gateway (8787)  ← 所有 LLM/Embedding 出口
                                               -> TokenHub / Ollama 等上游

PostgreSQL / Redis / Neo4j / Milvus / etcd / MinIO
```

### 2.2 暴露原则

| 模式 | 对外端口 | 说明 |
|---|---|---|
| 生产模式 | `frontend-gateway:80` | 唯一浏览器入口，其余后端、能力服务和基础设施仅容器内访问。 |
| 开发模式 | `80`、`8000`、`8001`、`8003`、`8004`、`8005`、`8006`、`8787`、`9621` | 通过 Compose override 或本机开发命令暴露，方便独立调试。 |

浏览器生产环境只进入 `frontend-gateway`。前端业务请求统一调用 `/api/v1/**`，由 Gateway 转发到 Sidecar，再由 Sidecar 调用具体 capability service。

### 2.3 服务职责

| 服务 | dev 端口 | 职责 |
|---|---|---|
| `frontend-gateway` | `80` | 聚合前端静态资源，反代 `/api/**`，设置基础安全头和缓存策略。 |
| `api_gateway` | `8000` | 外部 API 路由聚合、CORS、request id、基础日志、协议适配。 |
| `sidecar` | `8001` | 认证、租户上下文、内部 JWT、治理 hook、能力代理、流式代理。 |
| `llm-gateway` | `8787` | OpenAI 兼容代理，统一 LLM/Embedding 出口、Token 计量（Redis 实时 + PG 明细）。 |
| `knowledge-base-service` | `8003` | 知识库、文档状态机、RAG 接入、Neo4j 本体图、增强搜索、数据源接入。 |
| `atomic-rag` | `8004` | 多模态解析、RAG 任务、LightRAG 调用、`ontology_data` 产出。 |
| `business-domain-service` | `8005` | 领域空间、领域服务、引擎、适配器、技能、模板。 |
| `platform-service` | `8006` | 登录、RBAC、菜单、应用注册、审计、任务。 |
| `lightrag` | `9621` | 文档索引、RAG 检索、生成、WebUI。 |
| `neo4j` | `7474`/`7687` dev | 本体 ABox 图存储，承载 `:OntologyEntity` 节点和 `[:ONT_REL]` 边。 |

### 2.4 调用链

```text
Frontend
  -> API Gateway
    -> Sidecar
      -> Capability Service
        -> Service
          -> Repository
            -> PostgreSQL / Redis / Neo4j / External Engine

LLM/Embedding 调用统一经 llm-gateway:
  LightRAG / ontology_extractor / ontology_llm / 其他 LLM 调用方
    -> llm-gateway (8787)
      -> Token Swap（校验内部 token → 注入上游 key）
      -> 计量记录（Redis 实时 + PG 批量 + 结构化日志）
      -> 上游 LLM（TokenHub / Ollama 等）
```

这条链路是新后端开发的默认结构。历史平铺式 `service.py / dao.py / models.py` 不能作为新功能模板。

---

## 三、前端架构

### 3.1 前端工作区

`frontends/` 是 pnpm workspace monorepo，包含 4 个子应用和共享库。

| 目录 | 职责 |
|---|---|
| `frontends/shell` | 壳应用，负责登录后工作台、应用导航、路由守卫、子应用加载和统一上下文。 |
| `frontends/core-business` | 核心业务子应用，对应业务域能力入口。 |
| `frontends/platform-management` | 平台管理子应用，承载用户、角色、菜单、应用、权限、审计、任务等界面。 |
| `frontends/ecosystem-management` | 生态管理子应用，承载生态接入和外部集成界面。 |
| `frontends/shared/platform-theme` | 统一主题、Ant Design theme、CSS tokens、布局样式。 |
| `frontends/shared/shell-sdk` | 登录态引导、认证存储、跳转处理、standalone shell context、共享类型。 |

### 3.2 前端入口和路由

`frontend-gateway` 负责：

- 聚合 4 个子前端构建产物。
- 为 shell 和各子应用提供 SPA fallback。
- 反向代理 `/api/**` 到 API Gateway。
- 统一设置基础安全头、缓存策略和静态资源访问规则。

前端路由边界：

| 路由 | 归属 |
|---|---|
| `/`、shell 相关路径 | `frontends/shell` |
| `/core-business/**` | `frontends/core-business` |
| `/platform-management/**` | `frontends/platform-management` |
| `/ecosystem-management/**` | `frontends/ecosystem-management` |
| `/api/**` | 反代到 API Gateway |

生产应用清单的单一事实来源是平台后端应用注册表。shell 通过 `GET /api/v1/platform/frontend/apps` 获取应用清单，静态 `frontends/shell/public/app-manifest.json` 仅作为本地开发和后端不可用时的 fallback。

### 3.3 前端设计边界

- shell 负责统一登录态、导航结构、应用加载和跨子应用上下文。
- 子应用负责各自业务界面，不承担全局认证、全局导航和平台级状态。
- 共享主题、认证存储、跳转处理应放入 `frontends/shared`。
- 新子应用应接入 shell 和 `frontend-gateway`，不新增独立外部入口。
- 前端调用后端时默认使用 Gateway 路径，不绕过 Sidecar 直接访问业务服务。

---

## 四、后端架构

### 4.1 API Gateway

代码位置：`api_gateway/`

Gateway 是 HTTP 路由聚合层，职责包括：

- 挂载 `/api/v1/auth`、`/api/v1/platform`、`/api/v1/knowledge-base`、`/api/v1/business-domain`、TCADP 等路由。
- 处理 CORS、request id、基础日志和统一异常。
- 将业务请求转发到 Sidecar。
- 对文件上传、流式查询等入口做协议层适配。

Gateway 不应该实现业务 CRUD、拼接业务数据、补默认租户或替 capability service 执行业务权限判断。

#### 4.1.1 两种代理模式与分流规则

Gateway → Sidecar → 能力服务存在两种代理模式，二者都经过 Sidecar 治理（认证/租户/计量/限流/熔断），但契约不同：

| 模式 | 代表 | Sidecar 入口 | 业务路由位置 | 请求映射 |
|---|---|---|---|---|
| REST 透传代理 | `platform` | `/platform/{path:path}`、`/auth/*` | 目标容器自身 REST 路由表 | method/path/query/body 原样透传 |
| invoke action 契约 | `business_domain`、`knowledge_base` | `/invoke`、`/invoke/stream` | 能力内 `_build_dispatch` action 分发 + `execute()` | 固定 POST，body 为 `{capability_id, payload:{action,data}, tenant_id}` |

分流规则（新增/改造接口时据此选择，不要为了形式统一强行互换）：

- **platform 保持 REST 透传**：它承载认证引导（`/auth/login` 无 token、无租户）和无租户的平台共享元数据（`Application`/`Menu`/`Permission`/`SystemConfig`，走 `*_shared`）。invoke 契约强制 `verify_any_auth` + 必有租户，会把登录和共享元数据接口挡掉，因此 platform 不走 invoke。
- **业务能力一律走 invoke action 契约**：`business_domain`、`knowledge_base` 等租户业务能力统一经 `/invoke`，由能力内 action 分发承载业务路由，便于按 `capability_id/action` 维度做计量、限流、熔断和流式。
- 能力容器自身可能仍保留一套并行 REST `api/` 层（`register_routes` 挂在容器端口上），仅用于直连调试；**Gateway 链路不使用它**，以 invoke `execute()` 为唯一权威入口。
- 受控例外见 §6.4（知识库入站推送 `api_push`：免 JWT 公开端点 + 自包含 ingest key 解析真实租户）。

### 4.2 Sidecar

代码位置：`jonex_core/sidecar/`

Sidecar 是能力调用的治理入口，职责包括：

- 校验用户 JWT、测试 token 和 API Key。
- 解析并规范化租户上下文。
- 校验 JWT 与 `X-Tenant-ID` 的一致性。
- 生成内部服务 JWT。
- 执行计量、限流、熔断 hook。
- 按能力 ID 代理到具体 capability service。
- 支持普通调用、标准流式调用和 RAG 专用流式代理。

核心端点：

| 端点 | 说明 |
|---|---|
| `/health` | Sidecar 健康检查。 |
| `/capabilities` | 能力列表。 |
| `/auth/login`、`/auth/me`、`/auth/refresh` | 认证相关代理或无状态认证接口。 |
| `/platform/{path:path}` | platform service 反向代理。 |
| `/invoke` | 标准能力调用。 |
| `/invoke/stream` | 标准流式能力调用。 |
| `/invoke/stream/rag` | RAG 查询流式代理。 |

Sidecar 静态 fallback 配置：

| service key | 配置 |
|---|---|
| `platform` | `PLATFORM_URL` |
| `business_domain` | `BUSINESS_DOMAIN_URL` |
| `knowledge_base` | `KNOWLEDGE_BASE_URL` |
| `rag.lightrag` | `ATOMIC_RAG_URL` |

### 4.3 Capability Service

`capabilities/` 下的每个业务域都应视为独立 capability service。规范结构：

```text
capabilities/{capability_name}/
├── api/             # FastAPI route，只解析输入、提取租户、调用 service
├── models/          # SQLAlchemy 实体
├── repository/      # 数据访问层，继承 BaseRepository
├── services/        # 业务规则层
├── dtos/            # Pydantic v1 request/response 模型
├── contracts/       # 对外契约、能力声明或集成契约
├── integrations/    # 外部系统适配
└── workers/         # 后台任务
```

能力服务职责：

| 服务 | 主要职责 |
|---|---|
| `platform` | 登录、用户、角色、权限、菜单、应用、审计、任务调度。 |
| `business_domain` | 领域空间、领域服务、引擎、适配器、技能、模板。 |
| `knowledge_base` | 知识库、文档状态机、检索历史、RAG 接入、Neo4j 本体图、增强搜索、数据源接入。 |

---

## 五、能力体系

### 5.1 能力分层

```text
业务能力 Business Capability
  编排领域能力和原子能力，面向产品功能和可售卖能力

领域能力 Domain Capability
  复用业务场景能力，例如文本 RAG、语义检索、摘要等

原子能力 Atomic Capability
  技术组件能力，例如 LLM、向量、ASR、RAG、多模态解析
```

能力 ID 使用 `{类型}.{能力ID}.{版本}`：

| 示例 | 含义 |
|---|---|
| `business.knowledge_base.v1` | 知识库业务能力。 |
| `business.business_domain.v1` | 业务领域能力。 |
| `atomic.rag.lightrag.v1` | 基于 LightRAG 的 RAG 原子能力。 |

### 5.2 能力 SDK

代码位置：`jonex_core/capability/`

| 模块 | 职责 |
|---|---|
| `base.py` | `BaseCapability` 抽象基类。 |
| `models.py` | `CapabilityMetadata`、`CapabilityRequest`、`CapabilityResponse`、`CapabilityType`。 |
| `registry.py` | 能力注册中心。 |
| `locator.py` | 读取运行时清单，决定 local、remote、mock 调用方式。 |
| `atomic/` | LLM、vector、ASR、RAG、本体抽取等原子能力 client 和 adapter。 |
| `domain/` | 面向业务场景的领域能力。 |

### 5.3 本体知识引擎

知识库能力支持 RAG + 本体知识引擎。本体层面向强类型实体、关系和属性，作为 LightRAG 内部图谱之外的业务 ABox 存储。

核心设计：

- TBox 配置位于 `deploy/config/ontology/default.yaml`。
- atomic-rag 在解析和 RAG 入库后执行 Stage4 本体抽取，产出 `ontology_data`。
- atomic-rag 不直接依赖 Neo4j，只负责把本体抽取结果放入任务结果。
- knowledge-base service 读取 `ontology_data`，通过 `OntologyGraphRepository` 写入 Neo4j。
- Neo4j 中使用 `:OntologyEntity` 节点和 `[:ONT_REL]` 关系表达 ABox。
- 本体图按 `tenant_id`、`knowledge_base_id`、实体类型和规范名称进行隔离与合并。
- Neo4j 不可用时，知识库能力应降级到普通 RAG，不阻塞文档基础流程和检索能力。

知识库文档状态机由 `capabilities/knowledge_base/models/document.py` 中的 `DocStatus` 管理：

```text
pending -> parsing -> ready
                \-> failed
ready -> deleting -> deleted
              \-> failed
```

`capabilities/knowledge_base/capability.py` 启动 30 秒一次的异步对账循环，实际对账逻辑在 `ReconciliationService`：

| atomic-rag 状态 | knowledge-base 本地动作 |
|---|---|
| `completed` | 文档转 `ready`，写入 `rag_doc_ids`；若包含本体结果，则先写 Neo4j 再更新 PG 本体状态。 |
| `failed` | 文档转 `failed`，写入 `error_message`。 |
| `not_found` | 先尝试通过 LightRAG storage fallback 找回 `rag_doc_ids`；找不到则转 `failed`，提示用户删除后重传。 |
| `processing` / `pending` | 保持 `parsing`，仅记录 DEBUG 进度日志。 |

业务文档表与 LightRAG 内部存储分离，通过 `rag_task_id` 和 `rag_doc_ids` 做映射。

atomic-rag 的异步任务状态持久化在 Redis：

- key 格式为 `rag:task:{uuid}`。
- TTL 为 7 天，终态信息由 knowledge-base 对账回写到 PostgreSQL。
- `LightRAGAdapter._cleanup_orphan_tasks()` 在启动时扫描 `rag:task:*`，把 `pending` / `processing` 任务标记为 `failed`，避免容器重启后内存队列丢失导致文档长期卡在 `parsing`。
- 任务字段包含 `task_id`、`tenant_id`、`file_path`、`output_dir`、`status`、`progress`、`lightrag_doc_ids`、`error` 等。

TBox 由 YAML 管理，描述实体类型、关系类型和属性约束：

```yaml
# deploy/config/ontology/default.yaml
entity_types:
  - name: Organization
    aliases: ["公司", "企业", "机构"]
    attributes:
      - { name: legal_name, type: string }
relation_types:
  - name: BELONGS_TO
    source: Person
    target: Organization
```

本体状态机由 `OntologyStatus` 管理：

```text
pending -> extracting -> ready
                   \-> failed
```

- `pending`：文档解析完成但本体未抽取，或暂无可抽取候选实体。
- `extracting`：本体抽取进行中。
- `ready`：本体抽取成功，数据已通过 Cypher `MERGE` 写入 Neo4j。
- `failed`：本体抽取失败，`ontology_error` 记录原因。

`ReconciliationService.reconcile_ontology()` 定时扫描 `pending` / `failed` / `extracting` 文档，并通过 atomic-rag 的 `retry_ontology_extract` 触发重试，最多重试 3 次。

完整入库链路：

```text
上传落盘（本地 / COS 预签名直传）
  -> COS 下载（storage_backend=cos 时从 COS 下载到本地临时路径）
  -> Stage1 MinerU 解析
  -> Stage2 视频/音频转写：
       - 视频 + MPS_ENABLED + 存储在 COS：走腾讯云 MPS 视频智能分析（取代 ffmpeg+whisper ASR），
         失败即标 failed 不回退；保留 parse_video 元数据块
       - 其余视频/音频：ffmpeg 抽音轨 + whisper ASR（带 segment 时间戳切块）
  -> Stage3 推文本到 LightRAG（嵌入 + 图谱抽取，每块带 file_source 位置锚点）
  -> Stage4 本体抽取（ONTOLOGY_EXTRACT_ENABLED=true 时执行）
       -> LLM 按 TBox 归类、消歧、补属性
       -> 任务结果写入 ontology_data
       -> knowledge-base 对账写入 Neo4j
```

Neo4j schema 由 `jonex_core/common/neo4j_client.py` 的 `ensure_ontology_schema()` 在 knowledge-base 启动时幂等初始化：

| 名称 | 类型 | 作用 |
|---|---|---|
| `ont_entity_key` | 复合唯一约束 | 基于 `(tenant_id, kb_id, entity_type, canonical_name)` 确保实体 `MERGE` 幂等。 |
| `ont_entity_ft` | 全文索引 | 覆盖 `canonical_name` 和 `aliases_text`，支持中文实体检索。 |

增强搜索链路：

```text
用户查询
  -> SearchService.query_with_ontology
  -> Neo4j 全文索引检索候选实体
  -> 分数达到 ONTOLOGY_ROUTE_SCORE_MIN 时读取 1-hop 邻域事实
  -> LLM 基于事实回答
  -> 事实不足时回退普通 RAG
```

**编排推理链（P0 已实现）**：`query_with_ontology` 各阶段（本体匹配、路由决策、邻域取证、LLM 作答、RAG 兜底、多答案融合）通过 `ReasoningCollector` 结构化采集，以 `reasoning` 字段随响应返回。默认关闭（`with_reasoning=False`），双控安全（请求级开关 + 进程级 `REASONING_TRACE_ENABLED`）。详情见 [docs/reasoning-chain-design.md](docs/reasoning-chain-design.md)。

parse-result overlay：

- `ParseResultService` 读取 atomic-rag 解析结果。
- 实体列表可叠加 Neo4j 类型覆盖和 `ontology_typed` 标记。
- 关系列表可叠加 Neo4j 关系类型覆盖。
- 前端可同时看到原始解析结果和本体归类后的结构化视图。

本体详细执行计划和历史决策归档在：

- [docs/ontology-knowledge-engine-execution-plan.md](docs/ontology-knowledge-engine-execution-plan.md)

### 5.4 知识库数据源接入

knowledge-base 在「文件上传」之外支持多种数据源接入方式，KB 级实例统一登记在 `knowledge_base.knowledge_data_sources` 表，每个实例可引用定义页（`business_domain.data_access_methods`）的接入方式：

| `access_type` | 方向 | 说明 |
|---|---|---|
| `api` | 出站拉取 | 调外部 REST API（JSON 列表）拉文档，`services/ingestion/api_adapter.py`。 |
| `storage` | 出站拉取 | 连接外部 MinIO/S3 桶拉文档，`services/ingestion/storage_adapter.py`。 |
| `api_push` | 入站推送 | 对外暴露 OpenAPI 接收端点，外部用 ingest key 推送文档（见 §6.4）。 |
| `file` | 上传 | 既有文件上传方式。 |

- 出站同步（`api`/`storage`）：adapter 从外部源拉字节 → `get_object_storage().put_bytes()` 落平台存储 → 复用 `DocumentService.upload_document` 入库，与文件上传共用解析/本体编译管线。
- 外部存储数据源（客户的 MinIO/S3 桶）与平台存储后端（object_storage 抽象）是两个概念：前者是读取来源，后者是入库目的地。
- 平台存储后端支持 COS（腾讯云对象存储，生产）和 local（本地文件系统，开发回退），通过 `OBJECT_STORAGE_BACKEND` 环境变量切换。
- 凭据安全：外部 API token、S3 ak/sk 经 `jonex_core/common/crypto.py`（Fernet 对称加密）落库，列表/详情接口脱敏为 `***`。
- 一期范围：仅「立即同步」（定时同步需调度 worker，列为后续）、存储仅 MinIO/S3、API 仅 REST+JSON。
- 设计与逐文件执行计划见 `docs/knowledge-base-data-access-methods-plan.md`、`docs/data-access-backend-execution-plan.md`、`docs/data-access-frontend-execution-plan.md`。

---

## 六、租户与认证架构

### 6.1 租户原则

租户规范以 [backend-development-standard.md](backend-development-standard.md) 为准。架构层保留核心约束：

- 所有业务数据必须有合法 `tenant_id`。
- 禁止租户：空字符串、`default`、`default_tenant`、`system`。
- 外部普通业务 API 的请求 body 不得声明业务 `tenant_id`。
- Sidecar `/invoke` body 中的 `tenant_id` 只允许作为一致性校验，不是新的租户来源。
- API Key 只代表调用方身份，不代表业务租户；需要租户隔离时必须额外携带合法 `X-Tenant-ID`。
- 正式用户 JWT 由 Sidecar 解析，下游服务接收规范化后的 `X-Tenant-ID`。
- 本地开发和演示数据使用 `tenant_jonex_demo`。

### 6.2 租户提取链路

```text
普通业务请求
  -> Gateway route 校验 Authorization 存在
  -> Gateway route 从测试 token 或 X-Tenant-ID 提取当前租户，并随 /invoke 透传给 Sidecar
  -> Sidecar 解析正式 JWT / API Key / X-Tenant-ID，并校验租户一致性
  -> Sidecar 向下游转发规范化后的 X-Tenant-ID
  -> Capability route 再次 extract_tenant_id(request)
  -> Service / Repository 调用 require_tenant()
```

登录是认证引导链路，不走普通业务请求的强制租户前置规则：

```text
POST /api/v1/auth/login
  -> Gateway 原样转发请求体，并透传可选 X-Tenant-ID
  -> Sidecar 允许无租户代理到 platform
  -> Platform AuthService 按 X-Tenant-ID + username 精确登录
  -> 未携带 X-Tenant-ID 时，仅当 username 对应唯一活跃用户时自动确定 tenant_id
  -> 登录成功后 JWT 写入 user.tenant_id，后续请求以 JWT 租户为权威上下文
```

如果同一用户名存在多个活跃租户账号，登录必须要求用户选择租户后重试，不能随机选择或落入默认租户。

### 6.3 平台共享数据和租户数据

| 类型 | 示例 | 访问规则 |
|---|---|---|
| 平台共享元数据 | `Application`、`ApplicationRoute`、`Menu`、`Permission`、`SystemConfig` | 不带 `tenant_id`，repository 使用 `*_shared` 方法。 |
| 平台租户运行数据 | `User`、`Role`、`UserRole`、`RolePermission`、`AuditLog`、`TaskSchedule`、`LoginTicket` | 必须带合法 `tenant_id`。 |

业务能力数据默认都是租户数据，适用于 `business_domain`、`knowledge_base` 和新增业务 capability。

### 6.4 入站推送端点鉴权（api_push 例外路径）

`POST /api/v1/knowledge-base/ingest/{ds_id}` 是面向外部系统的**公开端点**，不使用平台用户 JWT，是租户提取链路的受控例外：

- 外部携带 `X-Ingest-Key`（数据源专属 key，格式 `yxk_<base64(tenant|kb|ds|random)>.<HMAC 签名>`，**自包含**租户/知识库/数据源，库内仅存签名哈希）。
- Gateway 用 `decode_ingest_key` 解出**真实 `tenant_id`** 并校验 key 内 `ds_id` 与 URL 一致，再经 Sidecar `invoke` 传入；Sidecar 对 `ingest_push` 这类系统 action（`_SYSTEM_INVOKE_ACTIONS`）采用该真实租户，**不使用 `system` 兜底**，限流/计量/审计照常按真实租户进行。
- 能力侧 `ingest_push` 用 `verify_ingest_key` 比对签名哈希做权威校验，并与 ds 记录交叉校验 tenant/kb/ds。
- 该路径仍经 Gateway → Sidecar，不破坏「前端不直连能力」与「禁止默认租户」原则。

---

## 七、数据与基础设施

### 7.1 PostgreSQL

PostgreSQL 是平台主数据存储，schema 边界：

| schema | 归属 |
|---|---|
| `platform` | 平台管理、认证、RBAC、菜单、应用、审计、任务。 |
| `business_domain` | 业务领域、领域服务、引擎、适配器、技能、模板。 |
| `knowledge_base` | 知识库、文档、解析状态、RAG 关联信息、数据源实例（`knowledge_data_sources`）。 |
| `metering` | LLM/Embedding Token 用量明细（`llm_usage_log`），由 llm-gateway 写入。`trace_id` 按业务请求归组、`request_id`（UNIQUE）做重试幂等去重。维度：tenant/user/scene/model/kb/doc。详见 `docs/llm-gateway-token-metering-execution-plan.md` §9 与 `Reference/LightRAG/JONEX_CHANGES.md`。 |

所有新增持久化业务实体应使用 `jonex_core/common/entity.py` 中的 mixin：

- `TenantMixin`
- `TimestampMixin`
- `SoftDeleteMixin`
- `AuditMixin`

### 7.2 Redis

Redis 用于：

- 服务注册和心跳。
- Sidecar 或能力服务的缓存。
- RAG 任务状态。
- 计量、限流、熔断状态扩展。

### 7.3 RAG 与本体基础设施

| 组件 | 用途 |
|---|---|
| `atomic-rag` | 多模态文档解析、任务持久化、调用 LightRAG、产出 `ontology_data`。 |
| `lightrag` | 文档索引、RAG 检索、生成、WebUI。 |
| `Neo4j` | 本体 ABox 图存储，提供实体全文检索和邻域查询。 |
| `Milvus` | 向量检索基础设施。 |
| `etcd` | Milvus 元数据依赖。 |
| `MinIO` | Milvus 对象存储依赖。 |

---

## 八、代码结构

```text
jonex-platform/
├── README.md                                      # 项目入口：定位、架构概览、快速启动、服务端口、文档索引
├── CLAUDE.md                                      # AI 协作与开发守则：工作目录、本地命令、分层边界、租户规则
├── AGENTS.md                                      # 同上（自动同步副本，与 CLAUDE.md 内容一致）
├── backend-development-standard.md                # 后端 capability service 开发规范
├── frontend-development-standard.md               # 前端 workspace、子应用、页面和 shared 包开发规范
├── dev-guide.macos.md                             # macOS/Linux 本地前后端调试指南
├── dev-guide.windows.md                           # Windows 本地前后端调试指南
├── PROJECT_ISSUES_AND_TODO.md                     # 项目议题、风险、开发状态和后续演进跟踪
├── jonex-platform-architecture.md                 # 稳定系统架构文档
├── Makefile                                       # Linux/macOS 启动、构建、日志、重建命令
├── jonex.ps1                                      # Windows PowerShell 启动、构建、日志、重建命令
├── main.py                                        # Sidecar 本地启动入口
├── run_gateway.py                                 # API Gateway 本地启动入口
├── run_llm_gateway.py                             # LLM 网关本地启动入口
├── pyproject.toml                                 # Python 项目与工具配置
├── requirements.txt                               # Python 依赖清单
├── .env.local.example                             # 本地调试环境变量模板（后端 + Atomic RAG）
├── .env.rag.local.example                         # LightRAG 专用环境变量模板
├── api_gateway/                                   # FastAPI API Gateway，负责外部协议入口和路由聚合
│   ├── main.py                                    # Gateway 应用入口
│   └── routes/                                    # Gateway 路由层，转发到 Sidecar 或能力服务
│       ├── auth.py                                # 登录、认证相关路由
│       ├── platform.py                            # 平台管理路由代理
│       ├── knowledge_base.py                      # 知识库路由代理
│       ├── business_domain.py                     # 业务领域路由代理
│       └── tcadp.py                               # 腾讯 TCADP 集成路由
├── jonex_core/                                    # 核心框架与共享基础设施
│   ├── capability/                                # 能力 SDK 与能力分层抽象
│   │   ├── base.py                                # BaseCapability 抽象基类
│   │   ├── registry.py                            # 能力注册中心
│   │   ├── locator.py                             # CapabilityLocator，支持 local/remote/mock 三态
│   │   ├── models.py                              # 能力元数据、调用请求和调用结果模型
│   │   ├── atomic/                                # 原子能力：LLM、向量、ASR、RAG、Ontology
│   │   │   ├── audio/                             # ASR 原子能力适配器
│   │   │   ├── llm/                               # LLM 原子能力接口与适配器
│   │   │   ├── vector/                            # 向量数据库原子能力接口与适配器
│   │   │   ├── rag/                               # LightRAG HTTP 适配器、存储读取、本体抽取器
│   │   │   └── ontology/                          # OntologyRegistry 与 TBox 数据模型
│   │   └── domain/                                # 领域能力：RAG 文本、语义检索、语音处理、摘要生成
│   ├── common/                                    # 通用工具：配置、数据库、仓储、租户、缓存、日志、异常、响应、Neo4j、本体 LLM、对象存储、文件来源工具
│   ├── discovery/                                 # Redis 服务发现与心跳注册
│   ├── integrations/                              # 外部系统集成适配器
│   │   └── tcadp/                                 # 腾讯 TCADP 适配器
│   ├── security/                                  # 内部 JWT、用户密码认证等安全模块
│   ├── sidecar/                                   # Sidecar 代理、认证治理 hook、流式代理
│   └── llm_gateway/                               # LLM 网关：OpenAI 兼容代理 + Token 计量
├── capabilities/                                  # 业务能力实现，按 capability service 独立演进
│   ├── platform/                                  # 平台管理能力：登录、RBAC、菜单、应用、审计、任务
│   │   ├── api/                                   # HTTP route
│   │   ├── auth/                                  # 平台认证与密码处理
│   │   ├── contracts/                             # 平台能力对外契约
│   │   ├── models/                                # SQLAlchemy ORM 实体和枚举
│   │   ├── repository/                            # 数据访问层
│   │   ├── dtos/                                  # Pydantic v1 request/response 模型
│   │   └── services/                              # 业务规则和事务编排
│   ├── business_domain/                           # 业务领域能力：领域空间、服务、引擎、适配器、技能、模板
│   │   ├── api/                                   # HTTP route
│   │   ├── dtos/                                  # Pydantic v1 request/response 模型
│   │   ├── integrations/                          # 领域能力外部集成
│   │   ├── models/                                # SQLAlchemy ORM 实体和枚举
│   │   ├── repository/                            # 数据访问层
│   │   ├── services/                              # 业务规则和事务编排
│   │   └── workers/                               # 后台任务与异步工作器
│   ├── knowledge_base/                            # 知识库能力：文档、检索、RAG、本体状态机、Neo4j 图谱、数据源接入
│       ├── api/                                   # HTTP route
│       │   ├── services.py                        # 知识库主路由
│       │   ├── spaces.py / folders.py             # 空间与文件夹路由
│       │   ├── tags.py / document_tags.py         # KB 级标签路由
│       ├── models/                                # SQLAlchemy ORM 实体和枚举
│       │   ├── document.py                        # 文档、DocStatus、ontology_status 等核心模型
│       │   ├── space.py / folder.py               # 空间与文件夹模型
│       │   ├── tag.py                             # 标签模型
│       │   ├── data_source.py                     # 数据源实例模型
│       │   ├── domain_service.py                  # 领域服务关联模型
│       │   ├── knowledge_info.py                  # 知识信息模型
│       │   ├── ontology_schema.py / ontology_synonym.py  # 本体 schema / 同义词模型
│       │   ├── parser_setting.py                  # 解析器设置模型
│       │   ├── search_feedback.py / search_history.py    # 搜索反馈与历史模型
│       ├── repository/                            # 数据访问层
│       │   ├── document_repository.py             # 文档仓储
│       │   ├── space_repository.py / folder_repository.py
│       │   ├── data_source_repository.py          # 数据源实例仓储
│       │   ├── ontology_graph_repository.py       # Neo4j 本体图仓储
│       │   ├── ontology_schema_repository.py / ontology_synonym_repository.py
│       │   ├── parser_setting_repository.py
│       │   ├── search_feedback_repository.py / search_history_repository.py
│       │   ├── domain_service_repository.py / knowledge_info_repository.py
│       ├── dtos/                                  # Pydantic v1 request/response 模型
│       │   ├── document.py / search.py            # 文档 / 检索 DTO
│       │   ├── parse_result.py                    # 解析结果和 overlay DTO
│       │   ├── reasoning.py                       # 编排推理链 Step / Trace DTO
│       │   ├── reference.py                       # 引用富化 DTO
│       │   ├── data_source.py                     # 数据源 DTO
│       │   ├── ontology_*.py                      # 本体 schema / synonym 等 DTO
│       ├── services/                              # 业务规则、状态机、RAG 和本体编排
│       │   ├── knowledge_base_service.py          # 知识库 facade
│       │   ├── document_service.py                # 文档上传、状态流转、删除
│       │   ├── space_service.py / folder_service.py
│       │   ├── search_service.py                  # 普通 RAG 与本体增强搜索（含推理链埋点）
│       │   ├── search_history_service.py / search_feedback_service.py
│       │   ├── parse_result_service.py            # 解析结果查询与本体 overlay
│       │   ├── ontology_service.py                # 本体抽取结果入图、查询和清理
│       │   ├── ontology_compiler.py               # TBox → 编译 schema
│       │   ├── ontology_query_service.py          # 多跳邻域查询
│       │   ├── ontology_synonym_service.py
│       │   ├── data_source_service.py             # 数据源 CRUD、测试连接、立即同步、入站推送
│       │   ├── domain_service.py / knowledge_info_service.py
│       │   ├── parser_setting_service.py
│       │   ├── reasoning_trace.py                 # ReasoningCollector 编排推理链采集器
│       │   ├── template_schema_provider.py        # 编译 schema 模板提供器
│       │   ├── reconciliation_service.py          # 异步对账与失败重试
│       │   └── ingestion/                         # 出站数据源接入适配器（api / storage）
│       └── capability.py                          # knowledge-base capability 注册入口
├── frontends/                                     # 前端 monorepo，pnpm workspace，4 子应用 + 共享库
│   ├── MANIFEST.md                                # 子应用清单与接入说明
│   ├── pnpm-workspace.yaml                        # pnpm workspace 配置
│   ├── package.json                               # 前端工作区脚本和依赖
│   ├── _template/                                 # 新子应用模板
│   ├── shell/                                     # 壳应用：统一登录、导航、路由守卫、子应用加载
│   ├── core-business/                             # 核心业务子应用
│   ├── platform-management/                       # 平台管理子应用
│   ├── ecosystem-management/                      # 生态管理子应用
│   ├── dev-gateway/                               # 本地开发 Node.js 网关（替代生产 Nginx）
│   └── shared/                                    # 跨子应用共享库
│       ├── platform-theme/                        # 统一主题、Ant Design theme、CSS tokens
│       └── shell-sdk/                             # 登录态引导、认证存储、shell 上下文和共享类型
├── deploy/                                        # Docker、Nginx、数据库、本体配置和部署说明
│   ├── config/                                    # 部署配置
│   │   ├── capability_runtime.yaml                # 能力 local/remote/mock 运行时清单
│   │   └── ontology/                              # TBox YAML 配置
│   ├── docker/                                    # Dockerfile 与构建辅助脚本
│   │   ├── sidecar.Dockerfile                     # Sidecar 镜像
│   │   ├── capability.Dockerfile                  # capability service 通用镜像模板
│   │   ├── gateway.Dockerfile                     # API Gateway 镜像
│   │   ├── llm-gateway.Dockerfile                 # LLM 网关镜像
│   │   ├── atomic-rag.Dockerfile                  # atomic-rag 镜像，含 raganything 和模型预下载
│   │   ├── atomic-rag-requirements.txt            # atomic-rag 依赖
│   │   ├── lightrag-source.Dockerfile             # LightRAG 源码自建镜像
│   │   ├── frontend-gateway.Dockerfile            # frontend-gateway 镜像，生产唯一对外 80 端口
│   │   ├── download_models.py                     # 构建期预下载 whisper、mineru、paddlex 等模型
│   │   └── frontend-entrypoint.sh                 # 前端容器入口脚本
│   ├── nginx/                                     # Nginx 子应用和网关配置
│   │   ├── app-locations.conf                     # 聚合前端静态资源并反代 /api（两种接入模式共用）
│   │   ├── modes/                                 # 监听层：server-http.conf / server-https.conf 二选一
│   │   ├── 50x.html                               # 上游不可用时的静态错误页
│   │   └── shell.conf                             # shell 子应用 Nginx 配置
│   ├── postgres/                                  # PostgreSQL 初始化与迁移
│   │   ├── init.sql                               # 初始 schema 创建
│   │   └── migrations/                            # 编号迁移脚本（001_schemas, 002_platform, 004_knowledge_base, 005_business_domain, 006_seed_data, 007_comments）
│   ├── redis/                                     # Redis 配置
│   │   └── redis.conf                             # Redis 配置文件
│   ├── docker-compose.yml                         # 通用/生产 Compose 基线
│   ├── docker-compose.override.yml                # 本地 Docker 联调覆盖
│   ├── docker-compose.debug.yml                   # 宿主机后端/RAG 单服务调试覆盖
│   ├── docker-compose.mac.yml                     # macOS CPU 开发覆盖
│   ├── docker-compose.gpu.yml                     # GPU/服务器部署覆盖
│   ├── start_capability.py                        # capability service 通用启动入口
│   ├── .env.example                               # 平台部署环境变量示例
│   ├── .env.rag.example                           # RAG/LightRAG 环境变量示例
│   ├── README.md                                  # 部署指南
│   └── DEPLOYMENT_ARCHITECTURE.md                 # 部署架构详细文档
├── docs/                                          # 执行计划、历史方案和专题设计文档（60+ 篇）
│   ├── ontology-knowledge-engine-execution-plan.md # 本体知识引擎执行计划、阶段任务和历史决策
│   ├── data-access-backend-execution-plan.md       # 数据源接入后端执行计划
│   ├── llm-gateway-token-metering-execution-plan.md # LLM 网关 Token 计量
│   ├── reasoning-chain-design.md                   # 编排推理链设计
│   ├── audit-logging-design.md                     # 审计日志设计
│   ├── knowledge-base-flow/                        # 知识库流程相关文档
│   ├── parse-compile-split/                        # 解析编译拆分相关文档
│   ├── old/                                        # 历史归档文档
│   └── superpowers/                                # Superpowers 技能相关
├── tests/                                         # 测试目录：unit、integration、e2e
├── Reference/                                     # 外部源码参考和本地集成依赖
│   ├── LightRAG/                                  # LightRAG 源码集成
│   └── Rag-anything/                              # RagAnything 源码参考
└── data/                                          # 本地开发数据卷目录
    ├── rag-inputs/                                # RAG 输入文件
    ├── rag-models/                                # 本地模型缓存
    └── rag-storage/                               # RAG 存储数据
```

---

## 九、配置与环境变量

核心配置来源：
- **本地调试**：根级 `.env.local`（后端 + Atomic RAG）和 `.env.rag.local`（LightRAG），通过 VSCode `envFile` 加载。
- **Docker 部署**：`deploy/.env`、`deploy/.env.rag` 和 Compose 环境变量。
- **代码层**：`jonex_core/common/config.py` 统一读取环境变量。

| 配置项 | 说明 |
|---|---|
| `ENV` | 运行环境：`dev`、`test`、`uat`、`prod`。 |
| `DB_HOST`、`DB_PORT`、`DB_USERNAME`、`DB_PASSWORD`、`DB_NAME` | PostgreSQL 连接配置。 |
| `REDIS_URL`、`REDIS_HOST`、`REDIS_PORT` | Redis 连接配置。 |
| `JWT_SECRET`、`JWT_ALGORITHM`、`JWT_EXPIRE_DAYS` | 用户 JWT 配置。 |
| `JWT_INTERNAL_SECRET` | 内部服务 JWT 密钥。 |
| `SIDECAR_URL` | Gateway 调用 Sidecar 的地址。 |
| `PLATFORM_URL` | Sidecar 调用 platform service 的地址。 |
| `KNOWLEDGE_BASE_URL` | Sidecar 调用 knowledge-base service 的地址。 |
| `BUSINESS_DOMAIN_URL` | Sidecar 调用 business-domain service 的地址。 |
| `ATOMIC_RAG_URL` | Sidecar 调用 atomic-rag service 的地址。 |
| `LIGHTRAG_API_KEY` | atomic-rag 调用 LightRAG 的 API Key。 |
| `CAPABILITY_RUNTIME_FILE` | 能力 local、remote、mock 运行时清单。 |
| `NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD` | knowledge-base service 访问 Neo4j 的连接配置。 |
| `ONTOLOGY_EXTRACT_ENABLED` | 是否启用 Stage4 本体抽取。 |
| `ONTOLOGY_SCHEMA_DIR` | TBox YAML 配置目录。 |
| `ONTOLOGY_ROUTE_SCORE_MIN` | 增强搜索命中本体路径的最低分。 |
| `REASONING_TRACE_ENABLED` | 编排推理链进程级总闸（默认开启）；前端再用 `with_reasoning` 按请求控制。 |
| `DATA_SOURCE_SECRET_KEY` | 数据源外部凭据对称加密密钥（Fernet，base64 urlsafe 32B）；未配置时从 `JWT_SECRET` 派生，仅限开发。 |
| `PUBLIC_API_BASE` | 生成 `api_push` 接收端点 URL（`ingest_url`）的对外基地址。 |
| `LLMGW_UPSTREAM_LLM_HOST` | LLM 网关上游 LLM 基地址。 |
| `LLMGW_UPSTREAM_LLM_API_KEY` | LLM 上游 API key，仅配在网关，其他服务不持有。 |
| `LLMGW_UPSTREAM_EMBED_HOST` | LLM 网关上游 Embedding 基地址。 |
| `LLMGW_UPSTREAM_EMBED_API_KEY` | Embedding 上游 API key。 |
| `LLMGW_INTERNAL_TOKENS` | 网关内部 token 白名单，逗号分隔。 |
| `LLMGW_METERING_ENABLED` | 是否启用 Token 计量（Redis + PG + 日志）。 |
| `LLMGW_PG_FLUSH_MAX_ROWS` | PG 批量写入阈值行数。 |
| `LLMGW_PG_FLUSH_MAX_SECONDS` | PG 定时刷新间隔秒数。 |
| `OBJECT_STORAGE_BACKEND` | 对象存储后端：`cos`（腾讯云 COS）或 `local`（本地文件系统，默认）。 |
| `COS_KEY_PREFIX` | COS 对象键前缀（目录），默认 `jonex`，所有文件统一存于此前缀下。 |
| `COS_REGION` | COS 地域（如 `ap-guangzhou`）。 |
| `COS_SECRET_ID` | COS 子账号 SecretId。 |
| `COS_SECRET_KEY` | COS 子账号 SecretKey。 |
| `COS_BUCKET` | COS 存储桶名称（如 `material-understand-1322124992`）。 |
| `COS_PRESIGN_EXPIRES` | 预签名 URL 过期秒数，默认 `900`。 |

---

## 十、新功能开发规范

新后端能力和新后端功能必须按照 [backend-development-standard.md](backend-development-standard.md) 创建；新前端子应用、新页面和新功能必须按照 [frontend-development-standard.md](frontend-development-standard.md) 创建。

### 10.1 后端分层规则

| 层 | 规则 |
|---|---|
| Gateway route | 只做协议入口和转发，不写业务规则。 |
| Capability route | 只解析请求、提取租户、调用 service。 |
| Service | 业务规则唯一归属，负责租户校验、资源归属、事务编排、异常转换。 |
| Repository | 继承 `BaseRepository`，租户实体查询必须包含合法租户过滤。 |
| Model | 业务实体默认继承统一 mixin。 |
| DTO | 对外请求和响应模型，不泄漏 ORM 内部对象。 |

### 10.2 前端接入规则

新增前端子应用时应：

- 放入 `frontends/` workspace。
- 接入 `frontends/shared/platform-theme`。
- 通过 `frontends/shared/shell-sdk` 复用登录态和认证跳转。
- 在平台后端应用注册表声明应用清单；shell 从 `GET /api/v1/platform/frontend/apps` 读取，静态 manifest 仅作本地 fallback。
- 在 `frontend-gateway` Nginx 配置中声明静态路由和 SPA fallback。
- 使用 `/api/v1/**` 调用后端，不直接访问后端容器端口。

### 10.3 数据规则

- 新业务实体默认带 `tenant_id`。
- 平台共享元数据必须明确列入共享清单，否则按租户数据处理。
- 禁止新增 `default`、`default_tenant`、`system` 作为业务租户。
- 本地开发和演示数据使用 `tenant_jonex_demo`。
- 新接口不得从 body 接收业务租户。
- 自定义 SQL 或 repository 查询必须显式包含租户条件和软删除条件。

### 10.4 本体开发规则

- TBox 修改优先进入 `deploy/config/ontology/default.yaml`。
- atomic-rag 只负责本体抽取产物，不直接写 Neo4j。
- Neo4j 写入、查询、删除和降级策略归属 knowledge-base service。
- 增强搜索必须保留普通 RAG fallback。
- parse-result overlay 只增强展示和检索，不应破坏原始解析结果。

---

## 十一、关键文件速查

| 模块 | 文件 | 用途 |
|------|------|------|
| 能力基类 | `jonex_core/capability/base.py` | 所有能力的抽象基类，含 `register_routes()` 自定义路由钩子 |
| 能力注册中心 | `jonex_core/capability/registry.py` | 全局能力注册与路由 |
| 能力定位器 | `jonex_core/capability/locator.py` | 运行时能力模式与端点解析（清单驱动） |
| 原子能力基类 | `jonex_core/capability/atomic/base.py` | 原子能力（LLM / 向量 / 音频 / RAG）基类 |
| 领域能力基类 | `jonex_core/capability/domain/base.py` | 领域能力（编排原子能力）基类 |
| RAG 客户端工厂 | `jonex_core/capability/atomic/rag/client.py` | RAGClient 抽象 + REMOTE / LOCAL / MOCK 工厂 |
| 本体注册中心 | `jonex_core/capability/atomic/ontology/registry.py` | TBox schema 加载 / 缓存 / 校验（YAML → OntologySchema） |
| 本体数据模型 | `jonex_core/capability/atomic/ontology/models.py` | 实体 / 关系类型定义、属性定义、消歧配置 |
| 本体抽取器 | `jonex_core/capability/atomic/rag/ontology_extractor.py` | Stage 4：LLM 本体抽取（归类 / 消歧 / 补属性） |
| Sidecar 应用 | `jonex_core/sidecar/main.py` | 统一 API 入口（认证 / 计量），含流式 invoke 路由 |
| Sidecar 反向代理 | `jonex_core/sidecar/proxy.py` | 能力服务 HTTP 反向代理，含流式代理 |
| 服务发现 | `jonex_core/discovery/registry.py` | 能力服务端点动态发现 |
| 心跳管理 | `jonex_core/discovery/heartbeat.py` | 心跳续约管理 |
| 内部服务认证 | `jonex_core/security/internal_auth.py` | 服务间 JWT Token 认证 |
| 用户认证 | `jonex_core/security/user_auth.py` | 密码哈希（bcrypt）+ 用户 JWT 签发 / 验签 |
| API 网关 | `api_gateway/main.py` | 对外公共 API 入口 + 中间件 |
| LLM 网关应用 | `jonex_core/llm_gateway/app.py` | FastAPI 应用工厂，注册 Token Swap 中间件 |
| LLM 网关路由 | `jonex_core/llm_gateway/router.py` | `/v1/chat/completions`、`/v1/embeddings`、`/metering/usage` |
| LLM 网关上游路由 | `jonex_core/llm_gateway/upstream.py` | 上游 host/key 解析、流式/非流式转发 |
| LLM 网关计量记录器 | `jonex_core/llm_gateway/recorder.py` | 三路落地：Redis 实时 + PG 批量 + 结构化日志 |
| LLM 网关 usage 抽取 | `jonex_core/llm_gateway/metering.py` | 从 chat/embedding/流式响应提取 token 用量 |
| LLM 网关上下文 | `jonex_core/llm_gateway/context.py` | 从 X-Jonex-* 请求头解析计量上下文 |
| LLM 网关认证 | `jonex_core/llm_gateway/auth.py` | Token Swap 中间件：校验内部 token → 注入上游 key |
| 能力服务启动脚本 | `deploy/start_capability.py` | 动态加载能力类 + 服务发现注册 + 心跳 |
| 本体图仓储 | `capabilities/knowledge_base/repository/ontology_graph_repository.py` | Neo4j Cypher MERGE + 全文检索 + 邻域查询 |
| Neo4j 客户端 | `jonex_core/common/neo4j_client.py` | Neo4j 异步驱动单例 + schema 初始化 |
| 本体问答 LLM | `jonex_core/common/ontology_llm.py` | `answer_from_facts()` 基于图谱事实回答 |
| 实体 mixin | `jonex_core/common/entity.py` | `TenantMixin` / `TimestampMixin` / `SoftDeleteMixin` / `AuditMixin` |
| 统一仓储基类 | `jonex_core/common/repository.py` | `BaseRepository`（租户隔离 + 软删除 + 分页） |
| 租户工具 | `jonex_core/common/tenant.py` | `extract_tenant_id` / `require_tenant` / `TenantContext` / `tenant_scope` |
| 配置管理 | `jonex_core/common/config.py` | 配置项管理 |
| 数据库 | `jonex_core/common/database.py` | SQLAlchemy 异步 DB + 租户上下文 |
| 缓存 | `jonex_core/common/cache.py` | Redis 缓存工具 + 租户隔离 |
| 日志 | `jonex_core/common/logger.py` | 结构化日志 + 请求 ID |
| 异常体系 | `jonex_core/common/exceptions.py` | 5 类业务异常（错误码 1xxx-5xxx） |
| 全局异常处理器 | `jonex_core/common/exception_handler.py` | FastAPI 异常 → 统一 JSON 响应 |
| 标准响应 | `jonex_core/common/response.py` | `StandardResponse` / `success_response` / `error_response` |
| 数据源加解密 | `jonex_core/common/crypto.py` | Fernet 凭据对称加解密 + ingest key 生成/签名/解析（`generate_ingest_key` / `verify_ingest_key` / `decode_ingest_key`） |
| 数据源服务 | `capabilities/knowledge_base/services/data_source_service.py` | 数据源 CRUD / 测试连接 / 立即同步 / 入站推送（`ingest_push`） |
| 数据源接入适配器 | `capabilities/knowledge_base/services/ingestion/` | `api`（REST+JSON）/ `storage`（外部 MinIO·S3）出站拉取适配器 |
| 对象存储工厂 | `jonex_core/common/object_storage/__init__.py` | `get_object_storage()` 单例工厂，按 `OBJECT_STORAGE_BACKEND` 返回 COS / 本地后端 |
| COS 对象存储 | `jonex_core/common/object_storage/cos_storage.py` | `CosObjectStorage`：腾讯云 COS 适配器（`put_bytes` / `get_to_path` / `presigned_url` / `presigned_put_url` / `head_object` / `delete`） |
| 本地对象存储 | `jonex_core/common/object_storage/local_storage.py` | `LocalObjectStorage`：本地文件系统适配器（开发回退） |
| 文件来源工具 | `jonex_core/common/file_source_util.py` | `build_file_source(task, idx, loc)` 构造带位置锚点的 file_source 字符串；`parse_file_source(raw)` 解析为结构化引用；`classify_media(mime, name)` 分类媒体类型；`to_location(r)` 从引用数据生成 `SourceLocation` |
| 引用 DTO | `capabilities/knowledge_base/dtos/reference.py` | `SourceLocation`、`SourceReference`、`ParsedRef`、`ReferenceResolveRequest` |
| 推理链 DTO | `capabilities/knowledge_base/dtos/reasoning.py` | `ReasoningStep` / `ReasoningTrace` + STAGE_/STATUS_ 常量 |
| 推理链采集器 | `capabilities/knowledge_base/services/reasoning_trace.py` | `ReasoningCollector` — 编排推理链采集器（enabled=False 时全 no-op） |
| 引用富化服务 | `capabilities/knowledge_base/services/search_service.py` | `_build_references()` / `_build_references_by_doc_ids()` / `resolve_references()` — 从 file_source 解析引用 → DB 富化 → COS 预签名 URL |

## 十二、架构结论

Jonex 平台的正系统架构应理解为：

```text
frontend-gateway + shell/子应用
  -> API Gateway
    -> Sidecar
      -> platform / business_domain / knowledge_base / atomic-rag
        -> PostgreSQL / Redis / Neo4j / LightRAG / Milvus / MinIO / COS
```

后续新功能直接按照最终规范开发：

- 前端进入 `frontends/` workspace 并接入 shell。
- 后端进入对应 capability service。
- 业务规则进入 service。
- 数据访问进入 repository。
- Pydantic v1 request/response 模型进入 `dtos`。
- 租户隔离使用 `extract_tenant_id`、`require_tenant`、`TenantContext`、`BaseRepository`。
- 跨服务调用通过 Sidecar，服务内部调用通过明确的 service/repository 边界。
