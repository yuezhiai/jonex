# deploy

`deploy/` 保存Jonex 平台部署相关文件，包括 Docker 构建上下文、Nginx 配置和 PostgreSQL migration。

## 目录

```text
deploy/
├── docker/                      # 服务镜像 Dockerfile
├── nginx/                       # frontend-gateway 与子前端 Nginx 配置
└── postgres/
    └── migrations/              # PostgreSQL 初始化和迁移脚本
```

## 部署入口

生产环境浏览器只访问 `frontend-gateway`：

```text
frontend-gateway:80
  -> shell 和子前端静态资源
  -> /api/** -> gateway:8000
  -> LLM 调用：lightrag / knowledge-base -> llm-gateway:8787 -> 上游 LLM
```

后端服务、Sidecar、能力服务、RAG 服务、PostgreSQL、Redis、Milvus、etcd、MinIO 默认只在容器网络内访问。开发环境可以通过 `docker-compose.override.yml` 暴露常规调试端口；需要把单个业务后端或 `atomic-rag` 切到宿主机调试时，使用 `docker-compose.debug.yml`。

## Nginx 文件

| 文件 | 职责 |
|---|---|
| `nginx/app-locations.conf` | 唯一前端入口的**业务路由**：聚合 shell、子应用、remote assets 和 `/api/**` 反代。新增子应用的路由加在这里。两种接入模式共用。 |
| `nginx/modes/server-http.conf` | 监听层：IP + HTTP，无 TLS。`ACCESS_MODE=http`（默认）时启用。 |
| `nginx/modes/server-https.conf` | 监听层：80 跳转 + 443 终止 TLS。`ACCESS_MODE=https` 时启用，需挂载证书。 |
| `nginx/50x.html` | 上游全挂时返回的静态 5xx 错误页。 |
| `nginx/expert-call.conf` | expert-call 子前端 standalone fallback 和 remote assets。 |

新增前端子应用时，需要同步：

- 子应用 Dockerfile。
- 子应用 `nginx/default.conf`。
- `deploy/nginx/app-locations.conf` 中的 standalone 路由和 remote assets 反代。
- 平台后端应用注册表。
- `frontends/shell/public/app-manifest.json` 本地 fallback。

## PostgreSQL migrations

迁移脚本规则：

- 新业务表默认带 `tenant_id`、时间戳、软删除字段，必要时带审计字段。
- 平台共享元数据不带 `tenant_id`，例如应用、菜单、权限、系统配置。
- 平台运行数据和业务数据必须带合法 `tenant_id`。
- 本地开发和演示数据使用 `tenant_jonex_demo`。
- 不写入默认业务租户。

## 常用命令

```bash
make build
make up
make ps
make logs
make down
make docker-local-up
```

- `make up`：加载 `docker-compose.override.yml`，适合本地整套 Docker 联调并暴露常规调试端口。
- `make docker-local-up`：加载 `docker-compose.debug.yml`，适合其他服务留在 Docker、单个业务后端或 `atomic-rag` 在宿主机调试。`lightrag` 会暴露 `9621` 方便访问，但容器内 `atomic-rag` 默认仍通过 `http://lightrag:9621` 调用它。

单服务：

```bash
make rebuild-service SERVICE=platform-service
make restart-service SERVICE=platform-service
make logs-service SERVICE=platform-service
```

前端镜像：

```bash
make rebuild-frontend-gateway
make rebuild-shell-frontend
make rebuild-expert-call-frontend
make rebuild-core-business-frontend
make rebuild-platform-management-frontend
make rebuild-ecosystem-management-frontend
```

## 构建加速（已融入 compose 构建）

构建优化已**直接整合进 `docker compose build`**：抽取共享基础镜像 `python-base`、用 `COMPOSE_BAKE` 委托 buildx 并行构建、保留 apt/pip/pnpm 缓存挂载。产出的就是 `docker compose up` 实际运行的 `deploy-*` 镜像，**不再有单独的一套 `jonex/*` 镜像**。运行时产物与优化前一致（依赖集合/端口/命令/健康检查/前端 dist 不变）。

工作方式：四个能力服务 + gateway/sidecar/llm-gateway 这 7 个后端镜像的 Dockerfile 改为 `FROM ${PYTHON_BASE}`，compose 中通过 `additional_contexts: python-base: docker-image://jonex/python-base:local`（命名上下文用 `python-base`，避免与 Dockerfile 内 `AS base` 阶段别名撞名）复用预构建的共享基础层。

### 一键构建

```bash
# *nix / CI：先构建 python-base，再并行 compose build（输出秒级总耗时）
bash deploy/scripts/build_all.sh
bash deploy/scripts/build_all.sh gateway    # 仅构建某个 compose 服务

# Windows（cmd）
deploy\scripts\build_all.cmd

# make（自动先构建 base，再 COMPOSE_BAKE 并行构建）
make build            # 本地联调（国内源：腾讯云 pip/apt、npmmirror、清华 uv）
make build-overseas   # 本地联调（国外官方源：pypi.org / deb.debian.org / registry.npmjs.org）
make build-gpu        # GPU
make build-prod       # 生产
make build-backend    # 仅后端
make build-service SERVICE=gateway
```

等价的手动两步（脚本/Make 已封装）：

```bash
# 1) 构建共享基础镜像并 load 进本地镜像库（被 7 个后端服务复用）
docker buildx build --load -t jonex/python-base:local -f deploy/docker/python-base.Dockerfile .
# 2) 并行 compose 构建（委托 buildx bake）
cd deploy && COMPOSE_BAKE=1 docker compose build
```

> `python-base` 不是 compose 服务，`docker compose up` 不会启动它；它只作为构建期的命名上下文被引用。首次/依赖清单变更后会重建该层，之后命中缓存。

### 优化构成

| 优化项 | 说明 |
|---|---|
| 共享基础镜像 | `python-base.Dockerfile` 收敛 7 个后端服务的公共层（时区 / 腾讯源 / apt / pip 依赖）|
| 并行构建 | `COMPOSE_BAKE=1` 让 `docker compose build` 委托 buildx bake，按依赖图并行 |
| 前端 pnpm store 缓存 | 5 个前端共享 `--mount=type=cache,id=jonex-pnpm-store`，依赖未变零下载 |
| atomic-rag 层固化 | 固定层顺序，源码层为最后 `COPY`，仅源码变更只重建 1 层 |

### 构建耗时度量（可选）

```bash
python deploy/scripts/build_benchmark.py --scenario cold --repeat 3 --baseline deploy/build-baseline.json
python deploy/scripts/build_benchmark.py --scenario incremental --repeat 3 --baseline deploy/build-baseline.json
```

### 进阶：CI 跨机缓存（可选）

如需在 CI 跨 runner 复用构建层缓存，可在 compose 各服务的 `build.cache_from` / `build.cache_to` 中声明 registry 或 GHA 缓存（配合 `docker-container` builder），`COMPOSE_BAKE=1 docker compose build` 会将其透传给 buildx。本地默认 `docker` 驱动不支持 cache 导出，无需配置。

### 验证测试（可选）

```bash
# 构建优化相关的轻量单元 / 快照测试（无需 docker，毫秒级）
uv run pytest tests/unit/test_python_base_dockerfile.py tests/unit/test_build_benchmark.py
```

> 优化是否生效，最直接的方式是 `make build`（或 `deploy\scripts\build_all.cmd`）后 `docker compose up -d` 做一次冒烟。

## 健康检查与日志

```bash
# 前端 Nginx 健康检查（生产唯一对外端口）
curl http://localhost/health        # 返回 'ok'

# 开发模式（compose override 暴露端口）下后端可直连
curl http://localhost:8000/health   # 网关
curl http://localhost:8001/health   # Sidecar
curl http://localhost:8002/health   # 专家访谈
curl http://localhost:8003/health   # 知识库
curl http://localhost:8005/health   # 业务域
curl http://localhost:8006/health   # 平台
curl http://localhost:8787/health   # LLM 网关

# 生产模式后端不对外，进容器访问
docker exec jonex-gateway curl -s http://localhost:8000/health
```

日志：

```bash
make logs                 # 全部
make logs-service SERVICE=knowledge-base-service
make logs-sidecar
make logs-postgres
```

应用日志同时挂载到 `jonex-logs` 数据卷。

## 数据库管理

```bash
# 连接 PostgreSQL
make shell-postgres
# 或：docker exec -it jonex-postgres psql -U jonex -d jonex

# 初始化 / 重建 schema 与种子数据
make init-db
```

迁移脚本位于 `postgres/migrations/`，按编号顺序执行（`001_schemas` → `002_platform` → `003_expert_call` → `004_knowledge_base` → `005_business_domain` → `006_seed_data` → `007_comments`）。`postgres/init.sql` 为容器首次启动的聚合初始化入口。

> 说明：`001` 创建全部 schema（platform/expert_call/knowledge_base/business_domain/metering）；计量表 `metering.llm_usage_log` 并入 `002`；知识库文档存储列、数据源表、本体编译快照的可编辑字段均已并入 `004`；对应种子并入 `006`。

若数据卷在新增某 schema 前已初始化，可手动补建：

```bash
docker exec -i jonex-postgres psql -U jonex -d jonex < postgres/migrations/004_knowledge_base.sql
```

### 增量 DDL（存量库迭代，不重建数据）

生产/已初始化的数据库在后续版本迭代时，表结构或字段变更不会自动应用（`/docker-entrypoint-initdb.d` 只在数据卷首次初始化执行）。所有增量变更沉淀为 `postgres/update/NNN_*.sql`，按编号顺序幂等执行，同时同步进 `postgres/migrations/`（全新库直接建齐，无需执行 update/）。

迁移须**人工显式触发**（`make up` 不会自动执行迁移）。执行状态记录在 `public.schema_migrations` 版本表：已应用文件跳过、不重复执行；执行成功才登记，失败中断不登记（修复后重跑将重试）。

```bash
# 预览待应用迁移（不执行）
make db-migrate-dry
# 交互确认后执行
make db-migrate
# 跳过确认直接执行（CI/自动化）
make db-migrate-yes
# 或直接：
bash deploy/postgres/update/apply.sh [--dry-run | --yes]
```

约定：

- `update/` 脚本面向存量库，全部用 `IF [NOT] EXISTS` / `ON CONFLICT DO NOTHING` / 幂等数据迁移，重复执行安全。
- 新增表结构时同时改 `migrations/` 对应全量 DDL，保证全新库与存量库最终态一致。
- 不要绕过 `apply.sh` 直连 `psql` 执行单个脚本——会绕过版本表登记，导致版本表与实际 schema 不一致。
- 破坏性变更（DROP 表/列、大范围 DELETE）发布前先 `make db-migrate-dry` 预览，由发布负责人评估后执行。

## GPU 加速（可选）

宿主机有 NVIDIA GPU 且已安装 `nvidia-container-toolkit` 时，叠加 `docker-compose.gpu.yml` 为 atomic-rag 启用 GPU：

```bash
make build-gpu     # 构建镜像
make up-gpu        # 启动（自动加载 gpu.yml）

# 验证 GPU 是否生效
docker exec jonex-atomic-rag python -c "import torch; print(torch.cuda.is_available())"
```

GPU 生效后 MinerU 解析器自动使用 CUDA，atomic-rag CPU 内存占用显著下降。

## 本体知识引擎运维

文档解析 + LightRAG 入库完成后，可选开启 Stage 4 本体抽取。当前职责划分：

- **atomic-rag** 负责抽取，产出 `ontology_data`（实体 / 关系），不直接写 Neo4j。
- **knowledge-base** 的对账服务（`reconciliation_service`）负责把 `ontology_data` 写入 Neo4j，再回写 PostgreSQL 的 `ontology_status` 状态机（先写 Neo4j、成功后置 `READY`，失败置 `FAILED`）。
- Neo4j schema 在 knowledge-base 启动时由 `ensure_ontology_schema()` 自动初始化，失败仅告警不阻塞服务。
- Neo4j 不可用时，知识库查询和文档 READY 流程降级到普通 RAG，不阻塞基础能力。

### 启用方式

在 `deploy/.env` 中开启抽取开关并重启 atomic-rag：

```bash
# deploy/.env
ONTOLOGY_EXTRACT_ENABLED=true
ONTOLOGY_SCHEMA_PATH=deploy/config/ontology/default.yaml

make restart-service SERVICE=atomic-rag
```

本体 TBox 定义见 `deploy/config/ontology/default.yaml`（实体类型、别名、属性、关系类型）。

### Neo4j 容器

```bash
# 约束检查
docker exec jonex-neo4j cypher-shell -u neo4j -p <your-neo4j-password> "SHOW CONSTRAINTS;"

# 查看本体实体
docker exec jonex-neo4j cypher-shell -u neo4j -p <your-neo4j-password> \
  "MATCH (n:OntologyEntity) RETURN n.tenant_id, n.entity_type, n.canonical_name LIMIT 10;"
```

### 增强搜索（本体优先）

```bash
curl "http://localhost:8000/api/v1/knowledge-base/documents/search/enhanced?query=腾讯&knowledge_base_id=KB1&mode=hybrid&top_k=3" \
     -H "X-API-Key: <your-api-key>"
```

返回 `{answer, source:"ontology"|"rag", ontology_instances:[...], rag_used:boolean}`：`source="ontology"` 表示基于 Neo4j 图谱事实 + LLM 回答；`source="rag"` 表示本体未命中、回退完整 RAG。

`ontology_status` 为 `pending`/`failed` 的文档由对账循环自动重试。

### 查询期思考分档变量（[jonex] ontology-query-thinking-latency-fix 方案）

严格模式升级策略为两档（快档 25s / 精档 120s），**思考作为档位维度**——
设计见 `docs/ontology-query-thinking-latency-fix-plan.md`（§3 两档定义、§7.2 回滚阶梯）。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `STRICT_MAX_ATTEMPTS_CAP` | `2` | 严格模式尝试上限（原 3 → 2，与两档结构一致；请求 `strict_max_attempts=3` 会被夹到 2，DTO 对外契约不变） |
| `ONTOLOGY_CROSS_RAG_TIMEOUT` | `20` | `_cross_verify` 对侧 RAG 校验整体超时（秒）；超时按「对侧无结果」保留本体原答案。**仅精档生效**（快档 `cross_verify=False` 跳过对侧）。取值由精档超时台账反推：15+60+20+8+5=108 < 120 |
| `LLMGW_DISABLE_THINKING_SCENES` | `lightrag_extract,ontology_extract,raganything_ingest,ontology_arbitration,ontology_qa_fast,rag_chunk_qa_fast,rag_fusion_fast` | 关思考白名单：抽取/裁决场景恒禁；`_fast` 变体=严格模式快档；原始查询 scene 不在列表=精档与非严格保留思考 |

⚠️ **思考模式下 `max_tokens` 是思考+正文共享预算**（上游 tokenhub 行为）：
思考链过长会吃光预算导致正文为空。本项目已在 `ontology_llm` 三个作答函数
把 `max_tokens` 提到 8192 并加 `reasoning_content` 兜底（`finish_reason=="length"`
时禁止兜底，防思考链泄漏）；`Reference/Rag-anything` 侧 `_metered_llm()` 已同源修复过一次。
**新 LLM 调用点若开思考必须显式考虑此预算关系，勿沿用 2048 级别的旧常量。**

**预算台账不变式**（守护测试 `tests/unit/test_ontology_thinking_scene.py::test_budget_ledger_invariant`）：
快档 8(邻域)+12(作答)+3 = 23 < 25；精档 15+60+20(对侧)+8(裁决)+5 = 108 < 120；
合计 145 < `STRICT_TOTAL_BUDGET`(150)。**改任一超时必须重算本台账**，并确认
前端/网关/Sidecar HTTP 超时 > 各档之和（代码默认 180s 覆盖精档 145s + 深度查询 180s；
`.env.local.example` 设 `GATEWAY_SIDECAR_TIMEOUT`/`SIDECAR_PROXY_TIMEOUT`=300 与 Docker 部署对齐）。

**回滚阶梯（细 → 粗）**：

1. 快档答案质量下降 → 白名单移除 3 个 `_fast`（纯配置）→ 全档恢复思考
2. 仅数值/单位计算类变差 → 加 `allow_common_sense` 强制精档判据（代码一行）
3. 档位表快档 `thinking` 改回 `True`（代码）→ 保留两档但都开思考
4. `STRICT_MAX_ATTEMPTS_CAP=3` + revert 档位表（回三档）
5. `LLMGW_DISABLE_THINKING_ENABLED=false`（总闸，连抽取场景一起恢复思考——慎用）

⚠️ 改白名单后须 `docker compose up -d llm-gateway`（`restart` 不重读 `env_file`）。

### 本体相关环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ONTOLOGY_EXTRACT_ENABLED` | `false` | 是否启用本体抽取 |
| `ONTOLOGY_SCHEMA_PATH` | `deploy/config/ontology/default.yaml` | TBox schema 路径 |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j 连接地址 |
| `NEO4J_USERNAME` | `neo4j` | Neo4j 用户名 |
| `NEO4J_PASSWORD` | `<your-neo4j-password>` | Neo4j 密码 |

### RAG 召回后处理变量（[jonex] rag-subject-filter 方案）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RAG_PRELLM_RERANK_ENABLED` | `false` | 送 LLM 前 chunk 级重排（经 llm-gateway /v1/rerank） |
| `RAG_PRELLM_RERANK_TOPK` | `8` | 重排后保留 top-K |
| `RAG_SUBJECT_FILTER_ENABLED` | `false` | 主体一致性信号总开关（方案 B 语义：软加权，永不删除候选） |
| `RAG_SUBJECT_WEIGHT` | `0.25` | 主体分权重 λ：`final = (1-λ)×rerank相关性 + λ×主体分` |
| `RAG_PLATFORM_ANSWER_ENABLED` | `false` | 方案 A 总开关：true 时 LightRAG 降为 retriever、平台侧 `answer_from_chunks` 作答（灰度） |
| `RAG_ANSWER_MAX_CONTEXT_CHARS` | `12000` | 送 `answer_from_chunks` 的 chunk 文本总长上限（硬截） |

> `RAG_STRUCTURE_AWARE_CHUNK` 已从 `.env.rag*` 移除：① 死代码（不在推送链路上，见表格治理方案 O5）；
> ② 配置位置错误——读取方 raganything 跑在 atomic-rag（env 源为 `deploy/.env`），
> 而 `.env.rag` 只被 lightrag 容器加载，该变量从未在任何环境真正生效过。
> 表标题回填能力由 `RAG_TABLE_GRID_V2` 的 `_resolve_table_caption` 接管。

> 模块级 `os.getenv`，改动后需重启 `knowledge-base-service`。

### 表格解析与切块治理变量（[jonex] table-parsing-retrieval-governance 方案）

⚠️ **配置位置**：以下变量由 **atomic-rag（raganything）进程**读取（compose `env_file: .env`），
须配在 `deploy/.env`（本地调试配 `.env.local`）；**不要**放进 `.env.rag*`（lightrag 容器专用，读不到）。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RAG_TABLE_GRID_V2` | `true`（未设置即生效） | 表格 L1 网格化 + L2 表头推断总开关；显式 `false` 回退旧 `normalize_table_rows`（首行即表头） |
| `TABLE_HEADER_MAX_LEN` | `40` | 表头长文本剔除阈值（字符）：超过或含句末符 → 判为表前说明写入 notes |
| `RAG_TABLE_CHUNK_MAX_CHARS` | `900` | 表格专用切块预算（字符，O1）：仅表格分支生效；文本链路仍用 `RAG_CHUNK_MAX_CHARS`（12000）由 LightRAG 按 token+overlap 切。tokenizer 校准：`scripts/calibrate_table_chunk_budget.py` |
| `RAG_LIGHTRAG_CHUNK_SIZE` | `1200` | 表格 chunk 超限断言阈值（token）：推送前 tokenizer 实测，超过 → WARNING + `table_stats.oversize_table_chunks`。**须与 `.env.rag` 的 `CHUNK_SIZE` 一致** |
| `XLSX_NATIVE_NORMALIZE` | `true` | xlsx 双路合并（L3）：openpyxl 表格主轨（日期/百分比/货币按 number_format 精确渲染）+ MinerU 图片/公式轨；false 全量交 MinerU |
| `RAG_PROMPT_LANG` | `zh` | 解析/摘要 prompt 语言（L4.2）：zh 中文模板（表格摘要列主体名+列名+行数、禁臆测）/ en 英文。进程级全局，影响所有模态 |
| `RAG_ASSET_UPLOAD_ENABLED` | `true` | 图片资产上传总开关（[jonex] image-refs 方案，不在表格治理范围内、同属 atomic-rag env）：入库时把文档图片上传到对象存储、file_source 携带 `aext`；`false` 只停资产上传，不阻塞主链路。设计见 `docs/image-reference-chain-execution-plan.md` |

改动后需重建 atomic-rag 镜像（`docker compose build atomic-rag`）并重启；
`PushChunksStage` 启动日志会打印开关生效值（防「以为回退了其实没回退」）。

### 文本块打包变量（[jonex] text-block-packing 方案）

⚠️ **配置位置**：同表格变量，由 **atomic-rag（raganything）进程**读取（compose `env_file: .env`），
须配在 `deploy/.env`（本地调试配 `.env.local`）；不要放进 `.env.rag*`。
设计见 `docs/text-block-packing-chunk-governance-plan.md`。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RAG_TEXT_BLOCK_PACKING` | `true`（未设置即生效） | 文本块打包总开关：相邻文本块按预算打包（标题作包头、跨页包记 pspans）；显式 `false` 回退逐块 1:1（灰度回退通道） |
| `RAG_TEXT_PACK_CHARS` | `1260` | 文本包字符预算（含包头）= 900 token × 1.4 系数固化（o200k_base 实测校准） |
| `RAG_TEXT_PACK_MAX_TOKENS` | `1200` | 文本包 token 红线：冲刷前 tokenizer 实测断言，超过 → WARNING + `oversize_text_chunks` 并按换算阈值兜底切分。**须与 `.env.rag` 的 `CHUNK_SIZE` 一致**（持平则不触发 LightRAG 二次切分） |
| `RAG_TEXT_PACK_HEADING_MAX_LEN` | `40` | heading 判定长度上限（C5 判据；C4 编号模式另有 `max(2×该值, 80)` 闸门） |
| `RAG_TEXT_PACK_DROP_NOISE` | `true` | 噪声块（页码/重复页眉）丢弃开关；`false` 改为吸附进包（零信息损失保守模式）。丢弃时每文档采样 WARNING 前 20 块原文+判据 |

另有一个**由 knowledge-base-service 进程读取**（同 compose `env_file: .env`）的配套变量——文本块打包方案的检索侧页段精算（片段级页码，设计见计划文档 §10）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RAG_REF_PAGE_SCORING_ENABLED` | `true` | 跨页打包 chunk 的引用页精算开关：true 时按 pspans 把 chunk 全文切页段、对 query 做关键词打分，取最高分页作 page_no（语义「与 query 最相关」）；false 或精算失败（无命中/完整性防御）回落 chunk 起点页，与改造前一致 |

### MCP Server 公网地址（mcp_config.url 来源）

统一 MCP Key 创建响应的 `mcpServers.jonex.url` 直接取自 `MCP_SERVER_PUBLIC_URL`，
无 localhost 兜底（代码锁定「无兜底」决策，本地需在 `.env.local` 显式填）。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MCP_SERVER_PUBLIC_URL` | `""`（空串） | MCP 创建响应 `mcpServers.jonex.url` 的取值；**不配置则 url 为空串**，客户端无法连接 MCP Server |

⚠️ **配置位置**：由 **platform-service 进程**读取（compose `env_file: .env`），
生产配在 `deploy/.env`，本地调试配 `.env.local`。

示例值（客户端可达的公网 HTTPS URL）：

```bash
MCP_SERVER_PUBLIC_URL=https://mcp.example.com
```

## 数据备份

```bash
# PostgreSQL
docker exec jonex-postgres pg_dump -U jonex jonex > backup_$(date +%Y%m%d).sql

# Redis
docker exec jonex-redis redis-cli BGSAVE
docker cp jonex-redis:/data/dump.rdb ./redis_backup.rdb

# Neo4j
docker exec jonex-neo4j neo4j-admin database dump neo4j --to-path=/backups
docker cp jonex-neo4j:/backups ./neo4j_backup
```

## 故障排查

服务无法启动：

```bash
make logs-service SERVICE=<service>     # 查看详细日志
make restart-service SERVICE=<service>  # 重启单个服务
make recreate-service SERVICE=<service> # 强制重建
```

数据库连接失败：

```bash
make ps                                 # 检查容器状态
make logs-postgres                      # 查看 PostgreSQL 日志
docker exec jonex-postgres pg_isready -U jonex
```

性能问题：

```bash
docker stats                            # 容器资源占用
make logs-sidecar
make logs-service SERVICE=knowledge-base-service
```

## 快速部署步骤

```bash
make init                # 初始化 .env / .env.rag（首次）
# 编辑 deploy/.env、deploy/.env.rag，确保两边 LIGHTRAG_API_KEY 一致
make build && make up    # 构建并启动本地 Docker 部署（加载 override）
make ps                  # 验证状态
make logs                # 查看日志
```

宿主机单服务调试使用 `make docker-local-up`。生产 / 服务器编排使用 `make build-prod` / `make up-prod` 或 `make build-server` / `make up-server`。

详细部署拓扑见 [DEPLOYMENT_ARCHITECTURE.md](DEPLOYMENT_ARCHITECTURE.md)。
