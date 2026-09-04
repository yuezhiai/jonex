# LightRAG Jonex（jonex）改动点清单

> 本文件记录Jonex 平台对 vendored LightRAG 源码的所有改动点，便于后续升级 LightRAG 时
> 快速定位、重新 apply。**所有改动均以 `# [jonex]` 注释标记**，可全局搜索 `[jonex]` 定位。

## 一、注释规范

- 单行改动：行尾加 `# [jonex]`。
- 代码块改动：块首加 `# ── [jonex] <说明> ───`，块尾可加 `# ── [jonex] end ───`。
- 新增参数/字段：行尾 `# [jonex]`，参数名统一加前缀 `_jonex_` 或 `X-Jonex-`（HTTP 头）。
- 新增整文件：文件头注释标明 `# [jonex] Jonex新增文件`。

## 二、改动分类

改动分两类：
1. **计量上下文透传（metering）**：让 LightRAG 内部发起的 LLM/Embedding 调用带上
   `X-Jonex-*` 头，使 llm-gateway 能记录 tenant/kb/doc/scene/trace 维度。
2. **其他功能改动（feature）**：与计量无关的业务定制。

---

## 三、计量上下文透传改动点（metering）

### 背景：两条透传链路（受 LightRAG 架构约束）

LightRAG 的调用分两种执行模型，决定了透传机制不同：

| 场景 | 执行模型 | 透传机制 | 可覆盖维度 |
|------|---------|---------|-----------|
| 入库抽取（document insert → extract） | **异步后台 pipeline** | 数据载体 `file_source`（编码进 chunk.file_path，随调用链带到 LLM） | tenant/kb/doc |
| 入库 embedding | **异步后台 pipeline** | （受限，见“待办/约束”） | scene 兜底 |
| 在线查询（/query, /query/stream） | **同步 HTTP 请求** | 请求级 `contextvar`（中间件读 `X-Jonex-*` 头 → contextvar → LLM/embed 调用注入） | tenant/kb/scene/trace |

> **关键约束**：入库是后台 pipeline，HTTP 请求接收即返回，真正的 LLM/embedding 调用
> 在独立 asyncio task 中执行，`contextvar` 无法跨 task 传递，因此入库路径只能靠
> `file_source` 这种“数据载体”透传，不能用中间件 + contextvar。

### 已落地的改动点

#### (A) file_source 链路 —— 覆盖【入库抽取 LLM】（异步 pipeline）

| 文件 | 位置 | 说明 |
|------|------|------|
| `lightrag/operate.py` | 抽取调用处（2 处） | 把 `file_path` 作为 `_jonex_file_source` 传入 |
| `lightrag/utils.py` | `use_llm_func_with_cache` 形参 `_jonex_file_source` | 透传给底层 `use_llm_func` |
| `lightrag/llm/openai.py` | `openai_complete_if_cache` 形参 `_jonex_file_source` | 经 `build_metering_headers` 解析 → `X-Jonex-*`，scene=`lightrag_extract` |

`file_source` 编码格式（由 atomic-rag 的 `lightrag_adapter.py` 构造）：
```
kb={knowledge_base_id}|doc={document_id}|tenant={tenant_id}|file={file_path}|chunk={idx}|trace={task_id}
```

#### (A2) contextvar-at-pipeline 链路 —— 覆盖【入库 embedding】（异步 pipeline）

| 文件 | 位置 | 说明 |
|------|------|------|
| `lightrag/jonex_metering.py` | 新增 `set_jonex_context_from_file_source()` | 从 file_source 解析 tenant/kb/doc/trace 写入 contextvar |
| `lightrag/lightrag.py` | `process_document` 入口（解析出 `file_path` 后） | 调 `set_jonex_context_from_file_source(file_path)` 写 contextvar |

原理：入库 pipeline 中每个文档由 `process_document` 在**独立 asyncio task** 内处理，入口
把 file_source 维度写入 contextvar。每个文档 task 的 context 互相隔离，并发入库不串租户。
仅当 file_source 解析出 tenant/kb/doc 任一维度才设置，裸路径（如 `unknown_source`）不污染。

> ⚠️ **修正（见 (A3)）**：早期认为「embedding upsert 子 task 在 `create_task` 时复制父
> contextvar 即可读到维度」——**该假设不成立**。`embedding_func` 被
> `priority_limit_async_func_call` 包成限流队列，真正执行 embedding 的是**启动期常驻
> worker**（context 为空），队列只传 args/kwargs 不传 context，故 contextvar 传不到 →
> `lightrag_embed` 行维度一度为空。已由 (A3) 让限流器透传 context 快照后**真正生效**。


#### (B) contextvar 链路 —— 覆盖【在线查询 LLM + 查询/入库 embedding】（同步 HTTP）

| 文件 | 位置 | 说明 |
|------|------|------|
| `lightrag/jonex_metering.py` | **新增文件** | contextvar 存取 + `build_metering_headers()` + `parse_file_source()` |
| `lightrag/api/lightrag_server.py` | `create_app` 内 `_jonex_metering_middleware` | 中间件：读入站 `X-Jonex-*`/`X-Request-ID` → contextvar |
| `lightrag/llm/openai.py` | `openai_complete_if_cache` 的 extra_headers 块 | 统一走 `build_metering_headers`，查询路径 scene 缺省 `lightrag_query` |
| `lightrag/llm/openai.py` | `openai_embed` 的 `embeddings.create` | 注入 `extra_headers`；scene 按 `context` 区分 `lightrag_embed_query` / `lightrag_embed`；入库 embedding 经 (A3) 限流器 context 透传后已带 tenant/kb/doc/trace |

#### (C) atomic-rag 侧注入（非 vendored，位于 `jonex_core/`）

| 文件 | 位置 | 说明 |
|------|------|------|
| `jonex_core/capability/atomic/rag/lightrag_adapter.py` | `_jonex_query_headers()` + `LightRAGServerClient.query/stream_query` | 调 LightRAG `/query`、`/query/stream` 时注入 `X-Jonex-Tenant-Id/Kb-Id/Scene/Trace-Id`，供 (B) 中间件读取 |

> scene 取值枚举：`lightrag_extract`（入库抽取）、`lightrag_query`（在线查询 LLM）、
> `lightrag_embed`（入库 embedding）、`lightrag_embed_query`（查询 embedding）、
> `lightrag_rerank`（检索期 rerank，与 query 同 task 树但独立 scene 分类）、
> `ontology_extract` / `ontology_qa`（平台侧本体调用，非 LightRAG）。

#### (D) rerank 计量注入 —— 覆盖【检索期 rerank】（同步 HTTP）

| 文件 | 位置 | 说明 |
|------|------|------|
| `lightrag/rerank.py` | `generic_rerank_api` HTTP 请求头 | `# [jonex] Gap E1`：调用 `build_metering_headers(default_scene="lightrag_rerank", force_scene="lightrag_rerank")` 注入 X-Jonex-* 头。force_scene 覆盖 contextvar 里的 `lightrag_query`，使 rerank token 能按独立 scene 分类，不被合并到查询 LLM 的 `lightrag_query` |
| `lightrag/jonex_metering.py` | `build_metering_headers` 新增 `force_scene` 参数 | `force_scene` 非空时无视 file_source / contextvar，强制使用指定 scene（优先级 force > file_source > contextvar > default_scene） |

> **设计要点**：检索期 rerank 与查询 LLM 在同一 HTTP 请求 task 树内执行，contextvar 已由
> server 中间件写入 `scene=lightrag_query`。若不强制覆盖，rerank 行的 scene 会落为
> `lightrag_query`，无法从 LLM token 中拆出 rerank 用量。`force_scene` 保证 `lightrag_rerank`
> 行可独立统计，同时 query LLM 仍正确归为 `lightrag_query`。

---

## 四、其他功能改动点（feature，非计量）

| 文件 | 位置 | 说明 |
|------|------|------|
| `lightrag/llm/ollama.py` | `ollama_model_complete` 附近 | `OLLAMA_LLM_ENABLE_THINK` 控制思考模式（默认关闭，加速实体抽取） |
| `lightrag/api/routers/query_routes.py` | `[jonex] 结构化查询` 端点 | 自定义结构化查询路由 |
| `lightrag/api/routers/document_routes.py` | `[jonex] 自定义 KG 端点` | 自定义知识图谱写入端点 |
| `lightrag/lightrag.py` | 入库 pipeline 单文件处理循环（`apipeline_process_enqueue_documents` 内，`Extracting stage`→`merge_nodes_and_edges`→`_insert_done`） | 单次推 chunk 各阶段耗时埋点：用 `time.perf_counter()` 分别测 extract / merge / persist，结尾打一条 `[jonex] chunk_timing` 日志（见下） |

### chunk_timing 耗时埋点说明（feature）

**目的**：把 `lightrag_upload_ms`（平台侧黑盒）拆开，输出 LightRAG 内部"一次推 chunk"
的 extract / merge / persist 三段耗时，便于定位瓶颈（实测 extract 占 90%+，即 LLM 实体/关系抽取）。

**改动点**（全部在 `lightrag/lightrag.py` 单文件处理循环内，均带 `[jonex]` 标记）：

| 锚点 | 新增变量/语句 | 含义 |
|------|--------------|------|
| `# Get document content from full_docs` 前 | `_jonex_t_extract_start = time.perf_counter()` | extract 计时起点（含分块+chunk向量化+LLM抽取） |
| `chunk_results = await entity_relation_task` / `file_extraction_stage_ok = True` 后 | `_jonex_extract_ms = ...` | extract 段耗时（ms） |
| `await merge_nodes_and_edges(` 前 | `_jonex_t_merge_start = time.perf_counter()` | merge 计时起点 |
| `# Record processing end time` 前 | `_jonex_merge_ms = ...` | merge 段耗时（写图库+向量库） |
| `await self._insert_done()` 前后 | `_jonex_t_persist_start` / `_jonex_persist_ms` | persist 段耗时（落盘） |
| `Completed processing file` 日志前 | `logger.info("[jonex] chunk_timing ...")` | 汇总日志，单行可 grep |

**输出样例**（stdout，`docker logs jonex-lightrag | findstr chunk_timing`）：
```
[jonex] chunk_timing doc=doc-xxx file=kb=...|doc=...|chunk=N|... extract_ms=43521 merge_ms=4480 persist_ms=18
```

**注意**：
- `time` 模块在 `lightrag.py` 已 import（原 `int(time.time())` 已在用），无新增依赖。
- 纯增量埋点，不改控制流/异常传播；三段变量都在成功路径上顺序赋值，到汇总日志时必已定义。
- 受 `MAX_PARALLEL_INSERT` 并发影响：多文件并行时 extract 段墙钟会因事件循环/上游限流抢占略偏大；要绝对精确需测 `_process_extract_entities` 内部，本次不做（粒度足够定位 extract vs merge vs persist）。
- 改完需重建镜像：`make rebuild-service SERVICE=lightrag`（或 `docker compose build lightrag` 后重启）。

---

## 五、已完成（方案 B）与已知约束

已完成：
- [x] **query 路径透传**：server 中间件 + contextvar，覆盖在线查询的 LLM 与 query embedding；
      atomic-rag 调 `/query`、`/query/stream` 注入 `X-Jonex-*` 头。
- [x] **scene 动态化**：`openai_complete_if_cache` 区分 `lightrag_extract` / `lightrag_query`。
- [x] **embedding 注入**：`openai_embed` 注入 scene（`lightrag_embed` / `lightrag_embed_query`）；
      query embedding 额外带 tenant/kb/trace。
- [x] **trace_id**：查询头透传 `X-Jonex-Trace-Id`（中间件回落 `X-Request-ID`）。

- [x] **trace_id 端到端**：
      - 在线查询：`knowledge_base` 的 `request.request_id`（= api_gateway 的 `X-Request-ID`）
        → `search/enhanced_search(trace_id=)` → `RAGClient.query(trace_id=)` → sidecar payload
        `trace_id` → atomic-rag `execute`（`payload.trace_id` 回落 `request.request_id`）
        → `adapter.query(trace_id=)` → `_jonex_query_headers` → LightRAG `X-Jonex-Trace-Id`。
      - 入库抽取：`file_source` 追加 `trace={task_id}` 字段，`parse_file_source` 解析后注入。
- [x] **user_id 端到端**：`_jonex_query_headers` 新增 `user_id` 参数 → `X-Jonex-User-Id`；
      capability → search_service → adapter → LightRAGServerClient 全链路透传；
      LightRAG 中间件 `set_jonex_context_from_headers` 已读 `X-Jonex-User-Id`（本已完成）。
- [x] **rerank 计量注入（E1）**：`generic_rerank_api` 调用 `build_metering_headers` 注入
      `X-Jonex-*`；`force_scene="lightrag_rerank"` 覆盖 contextvar 中的 `lightrag_query`，
      使检索期 rerank 按独立 scene 分类。依赖 (B) 链路 contextvar 已写入。

已知约束（按需求暂不实现）：
- ~~**入库 embedding 不精确到 kb/doc**~~ **【已解决，见 (A2)+(A3)】**：在 `process_document`
  入口把 file_source 维度写入 contextvar (A2)，并由限流器透传 context 快照到执行 worker
  (A3)，入库 embedding（`scene=lightrag_embed`）现已带 tenant/kb/doc/trace。注意：仅靠 (A2)
  不够——embedding 经 `priority_limit_async_func_call` 常驻 worker 执行，不继承父 task
  contextvar，必须配合 (A3) 才生效。每个文档独立 task，context 互不串租户。
- **query embedding 的 scene 细分**：未开启 asymmetric embedding 时，查询向量化内部 `context="document"`，
  归为 `lightrag_embed`（而非 `lightrag_embed_query`），但 tenant/kb/trace 正常。

---

## 六、按 workspace 硬隔离改造（跨知识库串库修复）

> 与上面的「计量」改动相互独立。关联设计文档：
> `docs/lightrag-workspace-isolation-execution-plan.md`。所有改动均带 `# [jonex]` 标记。

### 6.1 目的

上游 LightRAG Server 是**单实例 + 单 workspace**架构：`/query`、入库、图谱等端点都闭包
捕获启动期创建的唯一 `rag` 单例，**不消费**请求头 `LIGHTRAG-WORKSPACE`。导致所有知识库
落到同一默认 workspace，检索跨库召回、答案文本串库（引用层只能事后过滤 doc 级引用，
无法修正答案文本）。本改造让 Server 按 `LIGHTRAG-WORKSPACE` 头把检索/入库/图谱路由到
**对应 workspace 的独立 LightRAG 实例**，实现按「租户 + 知识库」硬隔离。

### 6.2 改动总览

| 文件 | 改动 |
|---|---|
| `lightrag/api/workspace_manager.py` | **新增**。`WorkspaceRAGManager`（按 workspace 懒加载实例注册表）+ 模块级 `get_workspace_from_request()` |
| `lightrag/api/lightrag_server.py` | `LightRAG(...)` 单例改为 `build_rag(workspace)` 工厂；lifespan 用 manager 初始化/关闭；路由注册改传 manager；`get_workspace_from_request` 改为从 `workspace_manager` 引用 |
| `lightrag/api/config.py` | 新增 `validate_workspace_isolation_config()`，启动时对 workspace 覆盖型环境变量 fail-fast |
| `lightrag/api/routers/query_routes.py` | `create_query_routes(manager, ...)`；`query_text`/`query_text_stream`/`query_data`/`query_structured` 加 `Request` 并按头解析实例 |
| `lightrag/api/routers/document_routes.py` | `create_document_routes(manager, ...)`；所有端点加 `Request` 并解析实例；**后台任务显式传解析后的 `rag`** |
| `lightrag/api/routers/graph_routes.py` | `create_graph_routes(manager, ...)`；所有图端点经 `_resolve_rag()` 解析实例 |
| `lightrag/api/routers/ollama_api.py` | `OllamaAPI(manager, ...)`；LLM-only 用默认实例，RAG 查询按头解析实例 |
| `deploy/.env.rag.example` | 补 workspace 覆盖变量留空警示；新增 `RAG_WORKSPACE_ISOLATION` / `RAG_WORKSPACE_CACHE_MAX` / `NEO4J_MAX_CONNECTION_POOL_SIZE` |

### 6.3 WorkspaceRAGManager（`workspace_manager.py`）

- **懒加载注册表** `workspace -> LightRAG`（`OrderedDict`）；首查时用 `build_rag(workspace)`
  工厂（捕获与默认实例完全相同的构造参数，仅 workspace 不同）创建并 `initialize_storages()`。
- **默认实例常驻**：`init_default()` 创建 `args.workspace`（通常为空）实例并
  `check_and_migrate_data()`，不参与淘汰，承接无头请求 / WebUI / `/status` / LLM-only。
- **per-workspace 锁**：避免并发首查重复构造/重复初始化（`initialize_storages` 幂等）。
- **LRU 淘汰**：超 `RAG_WORKSPACE_CACHE_MAX` 时 `finalize_storages()` 并移除；淘汰在 per-ws
  锁**外**执行避免锁嵌套死锁。`_eviction_lock` 串行化缓存结构变更；淘汰/关闭时清理 `_locks`。
- **特性开关**：`isolation_enabled=False` 时恒返回默认实例（行为同上游）。

路由统一用 `rag = await manager.get(get_workspace_from_request(request))`。
`get_workspace_from_request` 对头做 `[^a-zA-Z0-9_] -> _` sanitize（防注入），无头返回 `None`（回退默认）。

### 6.4 后台任务的 workspace 正确性（关键）

入库/扫描/删除走 `BackgroundTasks`，contextvar 在后台 task 会失效，**不能**依赖隐式上下文。
做法：在请求处理函数内（头仍可用）**同步解析出 `rag`**，再显式传给后台任务：

```python
rag = await _resolve_rag(http_request)          # [jonex]
background_tasks.add_task(pipeline_index_texts, rag, [request.text], ...)
```

模块级辅助函数（`pipeline_index_texts` / `run_scanning_process` / `background_delete_documents` 等）
本就以 `rag` 为参数，天然接收解析后的实例。

### 6.5 新增配置项（`deploy/.env.rag`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `RAG_WORKSPACE_ISOLATION` | `false` | `true` 启用隔离；`false` 回退单实例（兼容旧行为，便于灰度/回滚） |
| `RAG_WORKSPACE_CACHE_MAX` | `64` | 同时缓存的最大 workspace 实例数，超限 LRU 淘汰 |
| `NEO4J_MAX_CONNECTION_POOL_SIZE` | `16` | 单 Neo4j driver 连接池上限（有界，防隔离模式连接放大，见 6.7） |

### 6.6 配置前置条件与启动守卫（fail-fast）

`POSTGRES_WORKSPACE` / `PG_WORKSPACE` / `NEO4J_WORKSPACE` 会在驱动层强制覆盖 per-instance
workspace，把所有 KB 塌缩到同一空间、重新引入串库，**任何模式下都不可设置**。
`config.validate_workspace_isolation_config()` 在 `parse_args()` 末尾执行，检测到任一非空
即抛 `ValueError` 阻止启动。

### 6.7 资源与连接

- **PostgreSQL**：`ClientManager` 进程级按 DSN 共享连接池（ref_count），多实例**不**放大连接。
- **Neo4j**：⚠️ 每实例各自建 driver（无共享）。连接数上界 ≈
  `活跃实例数 × NEO4J_MAX_CONNECTION_POOL_SIZE`。Neo4j 为 `5.26-community` 单实例（1G 堆）。
  **已采取保险**：`NEO4J_MAX_CONNECTION_POOL_SIZE=16`（上游 fallback 是 100），最坏值从
  `64×100=6400` 压到 `64×16=1024`；连接池惰性建立，实际并发 ≈ 同时被查询的去重 workspace
  数。**未做**共享 driver（需改 vendored `neo4j_impl.py`，侵入大，属上规模再做的优化）。

### 6.8 已知局限与运维事项

1. **历史数据直接废弃**（平台决策）：切 `true` 后旧文档（默认 workspace / Neo4j `base` 标签）
   在 `tenant__kb` workspace 内查不到，按废弃处理，**无需任何迁移/重灌**。新文档落正确 workspace。
2. **Neo4j 连接**：已按 6.7 加上界保险，当前规模无需进一步处理。
3. **`/status`**：直接按头读 `pipeline_status`，不经 manager，不受隔离开关影响（无害）。
4. **淘汰残余窄窗口**：缓存满且某 workspace 恰被长查询使用时被选为 LRU 淘汰，理论上可能
   finalize 掉在用实例；触发条件苛刻，实践可忽略（代码注释已标注）。

### 6.9 启用 / 回滚

- **启用**：确认 6.6 三个覆盖变量未设置 → 设 `RAG_WORKSPACE_ISOLATION=true` 重启（旧数据按
  6.8.1 废弃，无需迁移）。建议先 1~2 个测试租户/KB 灰度。
- **回滚**：设 `RAG_WORKSPACE_ISOLATION=false` 重启即恢复单实例行为，无需回退代码。

### 6.10 校验

- 7 个改动文件均通过 `python -m py_compile`。
- 全仓无残留按旧签名（单个 `rag`）调用 `create_*_routes` / `OllamaAPI` 的入口。

### 6.11 WebUI workspace 切换器（调试用，默认关闭）

让 9621 WebUI 能按 `lightrag_workspace` 查对应库内容。WebUI 是静态 bundle（仓库不带源码，
docker build 时 `bun run build` 生成），故**不改 bundle**，复用 `SmartStaticFiles` 的运行时
配置注入钩子追加脚本实现。

**机制（cookie 方案，避免依赖 WebUI 用 fetch 还是 axios）**：

| 文件 | 改动 |
|---|---|
| `lightrag/api/lightrag_server.py` | 当 `WEBUI_WORKSPACE_SWITCHER=true` 时，在 `runtime_config_script` 后追加一段 `[jonex]` 脚本：页面右下角红色输入框 → 写 `lightrag_workspace` cookie → 刷新 |
| `lightrag/api/workspace_manager.py` | `get_workspace_from_request` 增加 **cookie 兜底**：头优先，无头且开关开启时读 `lightrag_workspace` cookie（同样 sanitize） |
| `deploy/.env.rag.example` | 新增 `WEBUI_WORKSPACE_SWITCHER=false`（默认关闭，含安全警示） |

浏览器自动把 cookie 带到 9621 所有请求（查询/文档/图谱），无需 patch fetch/XHR。

**新增配置项**：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEBUI_WORKSPACE_SWITCHER` | `false` | `true` 时 WebUI 出现 workspace 输入框 + 服务端读 cookie 兜底 |

**⚠️ 安全（务必遵守）**：9621 绕过平台 Gateway→Sidecar 的租户/权限校验。开关开启后，任何能
访问 WebUI 的人填入 `tenant__kb` 即可查**任意租户、任意 KB**——属跨租户数据暴露。因此：
默认 `false`；仅本地调试用；生产严禁开启，且 9621 严禁对外暴露（基线 compose 仅 `expose`，
仅 override/debug 才映射宿主）。`false` 时无 UI、不读 cookie，行为完全同现状。

---

## 八、限流器 context 透传（A3）—— 修复入库 embedding 维度实际未生效

> 关联设计：`docs/lightrag-embed-metering-context-propagation-plan.md`。所有改动带 `# [jonex]` 标记。

### 背景

(A2) 在 `process_document` 入口把 tenant/kb/doc 写入 contextvar，本意让入库 embedding 带上维度。
但实测 `scene=lightrag_embed` 行的 kb/doc 仍为空。根因：`embedding_func` 在 `LightRAG.__init__`
被 `priority_limit_async_func_call` 包成限流队列，真正执行 embedding 的是**启动期创建的常驻
worker**（context 为空），队列仅传 `args/kwargs` 不传 context，故 (A2) 设的 contextvar 传不到
worker。LLM 抽取不受影响是因为它走**显式 kwarg** `_jonex_file_source`（穿队列到 worker）。

### 改动点（`lightrag/utils.py` 的 `priority_limit_async_func_call`）

| 位置 | 改动 |
|------|------|
| 文件导入区 | 新增 `import contextvars` |
| `wait_func` 入队前 | `_captured_ctx = contextvars.copy_context()`，放入队列元组末尾（`PriorityQueue` 仅按 `(priority, count)` 排序，`count` 单调唯一，不会比较到 ctx） |
| `worker` 取队 | 解包出 `captured_ctx` |
| `worker` 执行 | `await func(*args, **kwargs)` → `await asyncio.wait_for(asyncio.create_task(func(*args, **kwargs), context=captured_ctx), timeout=...)`（`context=` 需 Python 3.11+，项目 3.12） |

效果：限流器承载的所有调用（LLM + embedding）都在**调用方 context 快照**中执行；入库 embedding
读到 (A2) 设的 tenant/kb/doc → `lightrag_embed` 行维度补齐。每次入队独立快照，并发不串。

### 取消语义提示

执行体由「直接 await 协程」改为「await 一个 `create_task` 包装的 task」，取消的是 Task 包装层。
`openai_embed` 的 `@retry` 只 catch `RateLimitError|APIConnectionError|APITimeoutError`，不命中
`CancelledError`，正式路径安全；若其他 provider retry 放宽为宽泛 catch 需留意（见方案 §6/§7）。

### context 生命周期

`lightrag.py` 的 `set_jonex_context_from_file_source` 未 reset Token——依赖「每篇文档独立
asyncio task、task 结束 context 自然消亡」的不变式（已在该处注释锁定）。**切勿在同一 task 内
复用处理多篇文档**，否则须保存 Token 并 `try/finally + clear_jonex_context`（方案 §4.6）。

### 生效前提

vendored 改动，须**重建 lightrag 镜像**：`docker compose build lightrag && docker compose up -d lightrag`。
验证：`docker exec jonex-lightrag grep -n "context=captured_ctx" /app/lightrag/utils.py`；入库新文档后
查 `metering.llm_usage_log` 的 `lightrag_embed` 行 kb_id/doc_id 非空。

---

## 九、图数据 HTTP 穷举端点（storage-source-http 改造）

> 关联设计：`docs/ontology-storage-source-http-refactor-plan.md`。所有改动带 `# [jonex]` 标记。

### 背景

存储升级（向量→Milvus、KV/DocStatus→PG）后，LightRAG 不再写本地 `vdb_*.json`，
atomic-rag 的 `LightRAGStorageReader`（读本地 JSON）读空 → 本体抽取「无候选实体」、
前端存储功能读空。改为经 LightRAG HTTP 端点穷举图数据（图仍在 Neo4j）。

### 改动点

| 文件 | 位置 | 说明 |
|---|---|---|
| `lightrag/kg/neo4j_impl.py` | import | `from typing import Any, Optional, final`（新增 Any/Optional） |
| `lightrag/kg/neo4j_impl.py` | `Neo4JStorage` 新增方法 | `_jonex_scope_conditions` / `jonex_get_entities_page` / `jonex_get_relations_page` / `jonex_get_graph_counts` / `jonex_get_graph_distribution`（均按 `_get_workspace_label()` 隔离） |
| `lightrag/api/routers/graph_routes.py` | `create_graph_routes` 内新增路由 | `GET /graph/entities`、`GET /graph/relationships`、`GET /graph/counts`、`GET /graph/summary`；`_require_jonex_graph` 守卫（非 Neo4j 后端返回 501） |

### 端点契约

- `GET /graph/entities?page&page_size&doc_id&file_path&keyword&entity_type&with_degree`
  → `{items:[{entity_name,entity_type,description,source_id,file_path,created_at,degree}], total, page, page_size}`
- `GET /graph/relationships?page&page_size&doc_id&file_path&keyword&source_entity&target_entity`
  → `{items:[{src_id,tgt_id,description,keywords,weight,source_id,file_path,created_at}], total, page, page_size}`
  （`source_entity`/`target_entity` 服务端精确过滤 `a.entity_id`/`b.entity_id`，total 同步反映过滤）
- `GET /graph/counts?doc_id&file_path` → `{entities_count, relationships_count}`
- `GET /graph/summary?doc_id&file_path` → `{total_nodes, total_edges, entity_type_distribution:{type:count}}`
- 全部读 `LIGHTRAG-WORKSPACE` 头做租户+知识库隔离（复用 `_resolve_rag`）。

### 过滤口径（与平台 file_source 对齐）

- `doc_id`：`n.file_path CONTAINS ('doc=' + $doc_id + '|')`（`doc=<id>|` 精确边界）。
- `file_path`：`$file_path IN split(coalesce(n.file_path,''), '<SEP>')`（合并实体多值精确成员相等，禁止裸 CONTAINS）。
- 度数：单条 Cypher `OPTIONAL MATCH (n)-[r]-() WITH n, count(r) AS degree`，`with_degree=false` 时省略。

### 生效前提

vendored 改动，须**重建 lightrag 镜像**：`docker compose build lightrag && docker compose up -d lightrag`。

---

## 十、chunk_id 透传 + 单片直查端点（RAG fallback 召回明细 / 查看单个 Chunk）

> 关联设计：`docs/rag-fallback-recall-detail-and-chunk-lookup-plan.md`。所有改动带 `# [jonex]` 标记。
> 生效前提：vendored 改动，须**重建 lightrag 镜像**。

### 10.1 chunk_id 透传（供 rag_fallback recalls 精确跳查）

`chunk_id`（`chunk-<md5>`）在 `/query` 返回的 `data["chunks"]` 里天然存在，但 `include_chunk_content`
富化只聚合 `content`、丢弃了 `chunk_id`。改动让 references 带出 `chunk_ids`：

| 文件 | 位置 | 说明 |
|---|---|---|
| `lightrag/api/routers/query_routes.py` | `ReferenceItem` 模型（约 L145） | **新增 `chunk_ids: Optional[List[str]] = None` 字段**。根因：`QueryResponse.references: List[ReferenceItem]`，FastAPI 按模型 schema 序列化，富化塞进 dict 的 `chunk_ids` 若模型未声明会被 Pydantic **静默丢弃**（`content` 因已声明得以保留）→ 下游 chunk_id 恒 null。补字段后才透出。 |
| `lightrag/api/routers/query_routes.py` | `/query` 非流式富化（约 L433-458） | 新增 `ref_id_to_chunk_ids` 映射，与 `ref_id_to_content` 同步收集 `chunk.get("chunk_id")`；富化时 `ref_copy["chunk_ids"] = ...` |
| `lightrag/api/routers/query_routes.py` | `/query/stream` 流式富化（约 L696-717） | 同上 |

向后兼容：仅新增 `chunk_ids` 字段，不改 `content` 富化；`chunks` 无 `chunk_id` 时为空数组，下游回退 null，不报错。

### 10.2 单片直查端点 `GET /documents/chunks/{chunk_id}`（查看单个 Chunk 内容）

| 文件 | 位置 | 说明 |
|---|---|---|
| `lightrag/api/routers/document_routes.py` | `create_document_routes` 内，`update_chunk` 端点前 | **新增只读端点** `@router.get("/chunks/{chunk_id}")`（完整路径 `/documents/chunks/{chunk_id}`）：`_resolve_rag(http_request)` 取 workspace-scoped rag → `rag.text_chunks.get_by_id(chunk_id)`，返回 `{chunk_id, content, full_doc_id, chunk_order_index, file_path, page_idx, line_start, line_end, tokens}`；chunk 不存在 → 404 |

> ⚠️ 装饰器必须是 `@router.get`（曾误挂到相邻 `@router.post` 上导致 405 Method Not Allowed）。
> 用途：kb-service 按 chunk_id 直查单片，替代原先"复用 get_doc_chunks 拉整篇再过滤"的错误路径。

**关联下游**（非本目录，登记以便追溯）：
- `Reference/Rag-anything/JONEX_CHANGES.md` §十四（raganything 侧 `get_chunk_by_id` + action + chunk_ids 透传）。
- `jonex_core/capability/atomic/rag/lightrag_adapter.py`（v1 兼容路径）、`client.py`（`RemoteRAGClient.get_chunk_by_id`）、
  `capabilities/knowledge_base/services/document_service.py`（`get_chunk` 直查）。

### 10.3 单文档状态直查端点 `GET /documents/{doc_id}`（P0-1 dup-failed 三态判定）

| 文件 | 位置 | 说明 |
|---|---|---|
| `lightrag/api/routers/document_routes.py` | `create_document_routes` 内，`/chunks/{chunk_id}` 与 `/chunks/update` 之间 | **新增只读端点** `@router.get("/{doc_id}")`（完整路径 `/documents/{doc_id}`）：`_resolve_rag(http_request)` 取 workspace-scoped rag → `rag.doc_status.get_by_id(doc_id)` 查 LightRAG 内部 doc-hash 主键，返回 `{id, status, content_summary, content_length, created_at, updated_at, track_id, chunks_count, error_msg, metadata, file_path}`；doc 不存在 → 404 |

用途：pipeline 轮询层（`stages.py` PushChunksStage）dup-failed 三态判定，按原件当前状态区分良性成功/可恢复等待/硬失败。无此端点时，所有 dup 因查不到原件状态而静默落入 hard_failed → strict 整体失败 → reparse 全量回滚。

> ⚠ 路由次序：`/{doc_id}` 注册在所有具名 GET 路由（`/pipeline_status`、`/track_status/{track_id}`、`/status_counts`、`/chunks/{chunk_id}`）**之后**，FastAPI 按注册序匹配，不抢具名路径。LightRAG 内部 doc-hash id 格式为 `doc-<md5>` 或 `dup-<md5>`，不会与现有具名路径冲突。


---

## content_summary varchar(4096) 溢出修复（bugfix）

**现象**：大 chunk（内容 > 4096 字符）入库时 LightRAG 报
`asyncpg.exceptions.StringDataRightTruncationError: value too long for type character varying(4096)`，
该 chunk 入库失败、被 `Preserving N failed document entries for manual review` 保留，反复重跑修不好
（内容 dedup 挡住重推）。

**根因**：`get_content_summary(content, max_length=4096)` 超长时返回 `content[:4096] + "..."` =
**4099 字符**，而 `LIGHTRAG_DOC_STATUS.content_summary` 列是 `varchar(4096)` → 溢出（off-by-3）。

**改动**（`# [jonex]` 标记）：

| 文件 | 位置 | 改动 |
|------|------|------|
| `lightrag/lightrag.py` | `apipeline_enqueue_documents` new_docs | `get_content_summary(..., max_length=4096)` → `4093`（留 3 字符给 "..."，双保险，兼容仍是 varchar 的旧列） |

**部署**：改的是 vendored LightRAG，需 `docker compose build lightrag && up -d lightrag`；
启动时自动迁移把既有 `content_summary` 列放宽为 TEXT。

**存量**：已失败的 chunk/doc 不会自动恢复（dedup + preserve-failed）；需删除对应 failed doc 后让文档重跑，
或走 LightRAG reprocess-failed。

---

## 十一、get_doc_by_file_path 去歧义修复 —— 优先返回 processed（非确定性重解析阻塞）

**现象**：重解析（reparse）某些已部分入库的文档时，`insert_text` 先查 `get_doc_by_file_path(file_source)`
判断是否存在。同一 `file_path` 下有 `processed` 原始记录 + 历史 `FAILED` dup 记录时，PostgreSQL
无 `ORDER BY` 取 `result[0]`，返回哪条由 PG 任意决定：
- 命中 `processed` → track_id 指向 processed track → track 查为 completed → 重解析成功。
- 命中 `FAILED` dup → track_id 指向 FAILED track → 严格模式 hard_failed → 整个 reparse 失败。

存量 dup-FAILED 越多，命中失败的概率越大 → **重解析非确定性被挡**。

**根因**：`get_doc_by_file_path`（`kg/postgres_impl.py`）查 `SELECT * ... WHERE file_path=$2`，
无排序。dup-FAILED 记录和 processed 原始共用同一个 `file_path`（chunk 的 `file_source`），
同 file_path 多条时 `result[0]` 非确定性。

**改动**（`# [jonex]` 标记）：

| 文件 | 位置 | 改动 |
|------|------|------|
| `lightrag/kg/postgres_impl.py` | `get_doc_by_file_path` SQL | `ORDER BY CASE WHEN status = 'processed' THEN 0 ELSE 1 END, created_at DESC`：processed 优先，同优先级取最新 |

**效果**：
- 同 file_path 有 processed + FAILED 多条时，**确定返回 processed**（重解析不再被历史 dup 随机挡）。
- 无 processed 只有 FAILED 时，行为同现状（返回 FAILED，后续按 FAILED 走 trigge-failed 逻辑）。
- JSON 和 MongoDB 实现暂未修复（项目仅用 PostgreSQL），升级 LightRAG 时一并补。

**部署**：vendored 改动，须 `docker compose build lightrag && up -d lightrag`。无 schema 变更。
