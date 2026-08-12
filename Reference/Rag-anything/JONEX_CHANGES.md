# RAG-Anything 悦溪（jonex）改动点清单

> 本文件记录悦溪平台对 vendored RAG-Anything（`Reference/Rag-anything/`）源码的改动点，
> 便于后续升级 RAG-Anything 时快速定位、重新 apply。**代码改动均以 `# [jonex]` 注释标记**，
> 可全局搜索 `[jonex]` 定位。约定与 `Reference/LightRAG/JONEX_CHANGES.md` 一致。

## 一、注释规范

- 单行改动：行尾加 `# [jonex]`。
- 代码块改动：块首 `# ── [jonex] <说明> ───`，块尾 `# ── [jonex] end ───`。
- 新增整类/整文件：类/文件头注释标明 `# [jonex] 悦溪新增`。

## 二、改动总览

| 分类 | 目的 |
|------|------|
| 解析器扩展 | 新增内网自建 MinerU（`mineru_selfhost`）解析器，去云化 + 去本地 GPU |
| 健壮性 | 多文件名/字段归一化去重与安全化 |
| 依赖分层 | 把 `mineru[core]` 从核心依赖降为 optional extra `local`，支撑 atomic-rag 镜像瘦身 |

> 说明：仓库中 `mineru_online`（mineru.net 云 API 解析器）、`raganything/asr/*`、
> `raganything/video_analysis/*`（含腾讯 MPS 后端）、`resilience`/`callbacks` 等模块亦为悦溪相关定制/新增，
> 本清单自「MinerU 内网自建接入」起开始系统记录；对应设计见
> `docs/mineru-selfhost-parser-execution-plan.md`。

---

## 三、解析器扩展（feature）

### (A) 新增 `MineruSelfHostParser`（内网 mineru-api 解析器）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/parser.py` | `class MineruSelfHostParser(MineruParser)` | `# ── [jonex] 悦溪新增：内网自建 MinerU (mineru-api) 解析器 ───` |

要点：
- 对接内网自建的 **MinerU 官方 `mineru-api`（FastAPI）** 服务（默认 `http://127.0.0.1:8000`；实际内网地址在 gitignored 的 `deploy/.env` 配），
  区别于 `MineruOnlineParser`（mineru.net 云 v4 签名上传契约）。
- 契约：`POST /tasks`（multipart 上传）→ 轮询 `GET /tasks/{id}`（`pending/processing/completed/failed`）
  → `GET /tasks/{id}/result`（`results.<stem>.content_list` 为 **JSON 字符串**）。
- 仅用标准库（`urllib` / `http.client`），**不依赖本地 mineru 包、不引入第三方 HTTP 库**。
- 复用父类 `MineruParser._FIELD_ALIASES` 做字段归一化；`return_images` 控制是否落盘图片并重写路径。
- 关键方法：`_post_multipart` / `_submit_task` / `_poll_task` / `_fetch_result_item`
  / `_build_content_list` / `_ascii_safe_filename` / `_dump_images`。

环境变量（parser 侧直接读 `os.environ`）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `MINERU_SELFHOST_BASE_URL` | `http://127.0.0.1:8000` | 内网 mineru-api 地址（实际值在 `deploy/.env`） |
| `MINERU_SELFHOST_BACKEND` | `pipeline` | `pipeline` / `hybrid-engine` 等 |
| `MINERU_SELFHOST_LANG` | `ch` | pipeline/hybrid 后端 OCR 语言 |
| `MINERU_SELFHOST_POLL_INTERVAL` | `5` | 轮询间隔（秒） |
| `MINERU_SELFHOST_POLL_TIMEOUT` | `1800` | 轮询总超时（秒） |
| `MINERU_SELFHOST_RETURN_IMAGES` | `false` | 是否拉取解析图片并落盘 |

### (B) 解析器注册（`mineru_selfhost` 接入内置解析器）

| 文件 | 位置（均带 `# [jonex]`） | 说明 |
|------|------|------|
| `raganything/parser.py` | `_BUILTIN_NAMES` | 加入 `mineru_selfhost`（禁止被 `register_parser` 覆盖） |
| `raganything/parser.py` | `SUPPORTED_PARSERS` | 加入 `mineru_selfhost` |
| `raganything/parser.py` | `list_parsers()` 映射 dict | 加入 `"mineru_selfhost": "MineruSelfHostParser"` |
| `raganything/parser.py` | `get_parser()` 分支 | `if parser_name == "mineru_selfhost": return MineruSelfHostParser()` |
| `raganything/parser.py` | `get_parser` docstring / `main()` argparse help / 顶部 description | 文案补 `mineru_selfhost` |

### (C) `_FIELD_ALIASES` 提取为类常量（去重，健壮性）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/parser.py` | `MineruParser._FIELD_ALIASES`（类常量，`# [jonex]`） | 原为 `_read_output_files` / `_read_any_output_files` 内 2 处重复局部 dict |
| `raganything/parser.py` | `_read_output_files` / `_read_any_output_files` | 改为引用 `cls._FIELD_ALIASES` |

### (D) 中文/非 ASCII 文件名安全化（健壮性）

- `MineruSelfHostParser._ascii_safe_filename()`：上传 multipart 时把非 `[A-Za-z0-9._-]` 文件名字符
  替换为 `_`（保留扩展名，MinerU 只按扩展名判类型），避免中文文件名在 `filename="..."` 头里被服务端
  错误解码；单文件任务结果按 `results` 首条兜底匹配，不依赖文件名。

### (E) 单测

| 文件 | 说明 |
|------|------|
| `tests/test_custom_parser.py` | `list_parsers` 数量断言 4→5 / 5→6；`test_register_rejects_builtin_name` 元组补 `mineru_selfhost`；新增 `TestMineruSelfHostParser`（注册 / 环境变量 / `_ascii_safe_filename` / `_build_content_list` / `_fetch_result_item`，全离线） |

---

## 四、依赖分层（build）：`mineru[core]` 降为 optional extra `local`

目的：让 atomic-rag 镜像在 online/selfhost 模式下可去掉 mineru 及其重传递依赖。**三处元数据一致修改**，
保证无论 pip 采用 pyproject 还是 setup.py 都生效：

| 文件 | 改动 |
|------|------|
| `pyproject.toml` | `[project].dependencies` 移除 `mineru[core]`；`[project.optional-dependencies]` 新增 `local = ["mineru[core]"]`（`# [jonex]`） |
| `setup.py` | `extras_require` 新增 `"local": ["mineru[core]"]`（`# [jonex]`） |
| `requirements.txt` | 移除 `mineru[core]` 行，加 `NOTE(jonex)` 说明其移入 `local` extra |

安装约定（由 `deploy/docker/atomic-rag.Dockerfile` 的 `RAG_PROFILE` 控制，见下）：
- `full`：`pip install -e "/opt/raganything[all,local]"`（含本地 mineru CLI）。
- `slim`：`pip install -e "/opt/raganything[image,text]"`（online/selfhost，无 mineru）。

> 安全依据：全仓无任何 `import mineru`，本地解析走 `mineru` CLI 子进程；online/selfhost 从不调用本地 mineru。

---

## 五、平台侧配套改动（不在本目录，登记以便追溯）

| 文件 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/lightrag_adapter.py` | 支持 `RAG_PARSER=mineru_selfhost`；记录 `self._parser_name`；CLI 专用 kwargs（backend/source）仅对 `RAG_PARSER=mineru` 注入；错误文案补三种模式 |
| `deploy/docker/atomic-rag.Dockerfile` | 新增 `ARG RAG_PROFILE=full`，第 2 层按 profile 条件安装 raganything（full=`[all,local]` / slim=`[image,text]`） |
| `deploy/docker/atomic-rag-requirements.txt` | 移除 `sentence-transformers`（全仓零引用；它与 whisper 是 torch 的唯二来源） |
| `deploy/docker-compose.yml` | `atomic-rag.build.args` 新增 `RAG_PROFILE: ${RAG_PROFILE:-full}` |
| `deploy/.env` / `deploy/.env.example` / `.env.local.example` | MinerU 段重构为三选一 + `MINERU_SELFHOST_*` + `RAG_PROFILE` 说明 |

详见 `docs/mineru-selfhost-parser-execution-plan.md`。

---

## 六、升级 RAG-Anything 时的重放清单

1. 全局搜索 `[jonex]` 定位 `raganything/parser.py` 内的：`MineruSelfHostParser` 整类、`_FIELD_ALIASES` 类常量、
   4 处注册点、docstring/CLI 文案。
2. 重放依赖分层：确认新版 `pyproject.toml` / `setup.py` / `requirements.txt` 是否仍把 `mineru[core]` 列为核心依赖；
   若是，重新降级到 `local` extra。
3. 跑 `tests/test_custom_parser.py` 校验解析器注册契约。
4. atomic-rag 侧按 `RAG_PROFILE=slim/full` 重建镜像并做 selfhost 端到端解析验证（见执行计划 §11）。

---

## 七、P1-4 结构感知切分（2026-08-05）

### 目的

对文档解析后的 `content_list` 做结构感知预分段：
- 版本清单/变更日志条目（`New in … X.Y.Z`）强制同 chunk
- 表格行回填文档/表标题上下文前缀，提升短行嵌入判别力
- 标签 `block_type`（list_entry/table_row/heading/text）供检索期过滤

### 改动点

| 文件 | 行 | 改动 |
|------|-----|------|
| `raganything/utils.py` | 导入 | 新增 `import os, re` |
| `raganything/utils.py` | `_classify_block_type()` | 新增函数，检测 block 结构类型 |
| `raganything/utils.py` | `_enrich_table_row_text()` | 新增函数，为表行回填标题上下文 |
| `raganything/utils.py` | `structure_aware_chunk()` | 新增函数，结构感知增强入口 |
| `raganything/utils.py` | `separate_content()` | 调用 `structure_aware_chunk()` 标注 `block_type` |

### 配套

| 文件 | 改动 |
|------|------|
| `Reference/LightRAG/lightrag/lightrag.py` | `ainsert_custom_chunks` 元数据合并新增 `block_type`/`entity_hint`/`text_idx`/`char_start`/`char_end` |
| `deploy/.env.rag` | 新增 `RAG_STRUCTURE_AWARE_CHUNK=false` |
| `docs/technology-kb-retrieval-quality-rootcause-and-optimization-plan.md` | 方案 §8 P1-4 |

### 升级重放

1. 全局搜索 `P1-4` 定位新增代码
2. 确认 `RAG_STRUCTURE_AWARE_CHUNK` 开关行为
3. 重建 `atomic-rag` 镜像验证


## 七、v2 LightRAG 关系响应契约归一化（fix）

| 文件 | 改动 |
|------|------|
| `raganything/service/http_lightrag_client.py` | `get_relationships()` 在 HTTP 客户端边界把 LightRAG 的 `src_id` / `tgt_id` 补齐为平台稳定契约 `source_entity` / `target_entity`，同时保留原字段，修复 v2 本体关系定型和 untyped fallback 均因端点字段缺失而得到 0 条关系的问题。 |
| `tests/test_service/test_http_lightrag_client.py` | 新增关系响应归一化回归测试，覆盖原始字段、已有规范字段及不修改上游响应对象。 |

升级重放：若新版 LightRAG 关系端点仍返回 `src_id` / `tgt_id`，保留该客户端边界映射；若上游已返回 `source_entity` / `target_entity`，当前兼容逻辑会优先使用规范字段。


## 八、v2 chunk 命名空间 token 注入回归修复（fix）

**现象**：v2 入库某文档后 LightRAG 图中该文档实体数为 0（`Task ... completed entities=0`），
本体阶段读不到候选实体 → `无候选实体` → 对账重试 3 次耗尽 → `ontology_status=FAILED`
（`reconciliation_service.py:481 Ontology retry limit reached (3/3)`）。

**根因**：v1 `LightRAGAdapter` 在 `upload_text` 前给每个 chunk 文本末尾注入
`<!--yx:{md5(tenant|kb|doc)[:8]}-->` 命名空间 token，使内容 hash 带上 (tenant, kb, doc)
隔离维度，避免 LightRAG 按 chunk 内容全局去重把跨文档/跨 KB 的相同文本合并（合并后只有首篇
文档抽到实体、其余文档图为空）。v2 迁移到 `PushChunksStage` 后**遗漏该 token**，
`_collect_text_chunks` / `_collect_multimodal_chunks` 上传的是原始文本；而消费侧
`task_manager.py` 本体抽取仍按 `<!--yx:[a-f0-9]{8}-->` 过滤该 token，证明契约期待其存在。

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | 新增 `_inject_ns_token(text, tenant_id, kb_id, document_id)`；`_collect_text_chunks` 文本/表格 chunk 与 `_collect_multimodal_chunks` 的 summary/video_frame/audio_segment chunk 追加该 token 后再入队上传（对齐 v1）。新增 `import hashlib`。 |

**验证**：`py_compile stages.py` 通过；需重建 atomic-rag 镜像生效。存量已 `entities=0` 的文档
需删除后重新入库（或清 LightRAG 该文档 chunk + 抽取缓存）才能重跑抽取，仅重置 `ontology_status`
无效（图仍为空）。

升级重放：若新版仍走 `PushChunksStage`，保留 `_inject_ns_token` 注入；消费侧过滤正则与
`hexdigest()[:8]` 长度需保持一致。


## 九、v2 ⇄ v1 行为补齐 #4/#5/#6 (2026-07-16)

补齐 v2 HTTP 模式相对 v1 `lightrag_adapter.py` 缺失的三项健壮性/成本/检索正确性行为。
详见 `docs/atomic-rag-v2-v1-parity-fixes-execution-plan.md`。

### (A) #6 逐 chunk 严格确认 doc_id/track_id

区分 TIMEOUT 与硬失败，默认 `RAG_REQUIRE_DOC_IDS=true`（与 v1 对齐）。

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage.__init__` 读取 `RAG_REQUIRE_DOC_IDS`（默认 true）；§4 track_status 轮询分类 terminal failed 为 `terminal_hard_failed`；§5 严格模式下硬失败/超时分别返回错误（`LightRAG 入库部分失败：X/Y` / `RAG_PUSH_TIMEOUT`）；ctx 写入 `total_chunk_count`/`failed_chunk_count`/`timeout_chunk_count` |
| `raganything/pipeline/base.py` | `PipelineContext` 新增 `total_chunk_count`/`failed_chunk_count`/`timeout_chunk_count`/`duplicated_chunk_count`/`total_pushed_count` |
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 失败分支持久化 `ctx.collected_doc_ids`→`task.lightrag_doc_ids`；透传 `timeout_chunk_count`；`_resume_track_polling` 区分 terminal failed vs still_pending → `timeout_chunk_count` |
| `raganything/service/models.py` | `TaskInfo` 新增 `timeout_chunk_count`/`duplicated_chunk_count`/`total_pushed_count` |

### (B) #5 全 duplicated 幂等守卫（跳过本体抽取）

所有 chunk 内容重复 → 跳过本体抽取（省 LLM 成本），设 `ontology_status="completed"`。

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage._push_one` 收集 `result.status == "duplicated"` 到 `duplicated_indices`；execute §5 计算 `duplicated_chunk_count`/`total_pushed_count` 写入 ctx |
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 本体触发前检查 `all_duplicated`（`total_pushed>0 and duplicated==total_pushed`）→ 跳过 `_run_ontology_extraction`、置 `ontology_status="completed"` |

### (C) #4 parse 解析阶段瞬时错误自动重试

仅对瞬时网络/SSL 错误指数退避重试，硬失败快速返回。

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | 新增 `_TRANSIENT_PARSE_ERROR_MARKERS` + `_is_transient_parse_error()` 辅助函数（对齐 v1）；`ParseStage.__init__` 读取 `RAG_PARSE_RETRY_MAX`（默认 3）/`RAG_PARSE_RETRY_BASE_SEC`（默认 2.0）；原有 try/except NotImplementedError 替换为 `_parse_with_retry()` 方法，primary/fallback 均套重试循环，每次重试前检查 `ctx.cancel_event` |

### 新增环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `RAG_REQUIRE_DOC_IDS` | `true` | 严格要求逐 chunk 确认 doc_id |
| `RAG_PARSE_RETRY_MAX` | `3` | 解析瞬时错误最大重试次数（含首次） |
| `RAG_PARSE_RETRY_BASE_SEC` | `2.0` | 解析重试指数退避基数 |

升级重放：三处改动均以 `# [jonex]` 标记，集中在 `stages.py`/`base.py`/`task_manager.py`/`models.py`。


## 十、v2 batch_track_status pending 脏残留误判超时修复（2026-07-16）

**现象**：文档入库成功（collected_doc_ids 有值），但 PushChunksStage 报 `RAG_PUSH_TIMEOUT: 600.0s`，
而实际轮询几秒就退出了，并未等待 10 分钟。

**根因**：`batch_track_status` 的 `pending` dict 在循环外声明、循环内不清理。一个 chunk 第 1 轮
还在 `processing`（写进 `pending`），第 2 轮才 `completed`（写进 `terminal`）——但没有任何地方把它从
`pending` 里删掉。结果它同时留在 `terminal` 和 `pending`。`stages.py` §5 按 `pending_track_ids`
判超时，把已完成的 chunk 误判为 timed_out → 严格模式直接报 `RAG_PUSH_TIMEOUT`。

**修复**：终态 track 进 `terminal` 时同步从 `pending` 剔除（`pending.pop(tid, None)`），一行核心改动。

| 文件 | 改动 |
|------|------|
| `raganything/service/http_lightrag_client.py` | `batch_track_status` 轮询循环内 `terminal[tid] = result` 后追加 `pending.pop(tid, None)`（`# [jonex]`） |
| `tests/test_service/test_http_lightrag_client.py` | 新增 `TestBatchTrackStatusNoDirtyPending`：跑真实 `batch_track_status` 实现（mock `_poll_one_track`），验证 processing→completed 转换后 pending 不含已完成 track |

升级重放：若新版 `batch_track_status` 重构轮询逻辑，需确保终态 track 不会残留在 pending/未完成集合中。


## 十一、v2 batch_track_status 并发控制（2026-07-16）

**现象**：V2 `batch_track_status` 用 `asyncio.gather` 一次性并发所有 remaining track_ids，
大文档数百 chunk 时同时建立数百 HTTP 连接，无任何限流机制。V1 有 `RAG_TRACK_POLL_CONCURRENCY=8` 来控制。

**修复**：`HttpLightRagClient.__init__` 读取 `RAG_TRACK_POLL_CONCURRENCY`（默认 8），
`batch_track_status` 轮询循环内用 `asyncio.Semaphore` 包装 `_poll_one_track`，限制同时进行的
track_status 查询数。

| 文件 | 改动 |
|------|------|
| `raganything/service/http_lightrag_client.py` | `__init__` 新增 `self._track_poll_concurrency`；`batch_track_status` 用 semaphore 限流（`# [jonex]`） |

### 新增环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `RAG_TRACK_POLL_CONCURRENCY` | `8` | track_status 轮询并发上限 |

升级重放：若新版 `batch_track_status` 重构轮询逻辑，保留 semaphore 限流避免连接风暴。


## 十二、parser.py 拆包后 CLI 顶部 description 文案补回（review, 2026-07-17）

**背景**：`MineruSelfHostParser` 原在单体 `raganything/parser.py`（commit `243dd4c`）实现，
后 `parser.py` 重构为 `raganything/parsers/` 包（`mineru.py` / `registry.py` / `__init__.py`）。
本次 review 逐方法比对拆包后实现与 `243dd4c` 原始实现，核心类、`_FIELD_ALIASES`、4 处注册点、
`__init__` 导出、`--parser` argparse help 均已完整保留，**仅顶部 `ArgumentParser(description=...)` 漏补**
（见上文 §三(B) 记录的「顶部 description」项，拆包时只补了 `--parser` help，遗漏了顶部 description）。

| 文件 | 位置 | 改动 |
|------|------|------|
| `raganything/parser.py` | `main()` 内 `argparse.ArgumentParser(description=...)` | description 由 `"...MinerU online, Docling, or PaddleOCR"` 补为 `"...MinerU online, MinerU self-host, Docling, or PaddleOCR"` |

纯 CLI 帮助文案，不影响运行时解析行为。`py_compile` `parser.py` / `parsers/{mineru,registry,__init__}.py` 全部通过。

升级重放：全局搜索 `[jonex]` 及本节，确认 `parser.py` 顶部 description 与 `--parser` help 两处文案都含 `mineru_selfhost` / `MinerU self-host`。


## 十三、P3 push_chunks 阶段信号 + VLM base64 caption 适配器（2026-07-23）

批次 2-A + 2-C：透出 P3 推送入图阶段信号供 kb-service 对账置 INGESTING；
修复图片 VLM 描述调用约定不匹配（base64 vs file://）。

详见 `docs/kb-doc-status-ingesting-and-patrol-plan.md` §4、§6、§12。

### (A) PushChunksStage 发 on_push_chunks_start → current_step="push_chunks"

| 文件 | 改动 |
|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage.execute` 起始 dispatch `on_push_chunks_start`（`# [jonex] Callback: push_chunks start`） |
| `raganything/callbacks.py` | `ProcessingCallback` 新增 `on_push_chunks_start` 空实现（`# [jonex] 批次 2-A`） |
| `raganything/service/task_manager.py` | `ProgressTrackingCallback` 新增 `on_push_chunks_start` 处理器：`current_step="push_chunks"`、`progress=0.92`（`# [jonex] 批次 2-A`） |

信号值定稿为 `"push_chunks"`，经 `get_task_status` 内存实时透出，kb-service 对账据此置 `DocStatus.INGESTING`。

### (B) VLM base64 caption 适配器

| 文件 | 改动 |
|------|------|
| `raganything/models/adapters.py` | 新增 `base64_caption_adapter(bound)`：签名 `(prompt, image_data=base64, system_prompt=...)` → `data:` image_url（`# [jonex] 批次 2-C`） |
| `raganything/models/__init__.py` | 导出 `base64_caption_adapter` |
| `raganything/service/model_factory.py` | `build_vlm()` 返回值从 `Callable` 改为 `dict {"func": ..., "bound": ...}` |
| `raganything/processor_builder.py` | `_create_image` 优先用 `_vlm_bound` + `base64_caption_adapter`，fallback 到原有 `_vlm_func` |
| `raganything/raganything.py` | 新增 `vlm_bound` 字段；`_init_processors_via_builder` 传递 `vlm_bound` 到 builder |
| `atomic-rag-server-v2.py` | 解构 `build_vlm()` 返回 dict，传 `vlm_bound` 到 `RAGAnything` |

升级重放：若新版 `PushChunksStage.execute` 重构，保留起始 dispatch `on_push_chunks_start`；
若新版 `build_vlm()` 签名变化，确认 `vlm_bound` 仍能从 `model_factory` 传递到 image processor factory。

### (C) 平台侧配套改动

| 文件 | 改动 |
|------|------|
| `jonex_core/capability/atomic/rag/lightrag_adapter_v2.py` | `_assemble_stack` 解构 `build_vlm()` 返回 dict，传 `vlm_bound` 到 `RAGAnything` |
| `atomic-rag-server-v2.py` | 同上，解构 `build_vlm()` 返回 dict，传 `vlm_bound` 到 `RAGAnything` |

## 十四、chunk_id 透传 + 单片直查（RAG fallback 召回明细 / 查看单个 Chunk）（2026-07-24）

> 关联设计：`docs/rag-fallback-recall-detail-and-chunk-lookup-plan.md`（§3.2、§9 P9~P11）。
> 关联 LightRAG 侧改动：`Reference/LightRAG/JONEX_CHANGES.md` §十。
> 生效前提：vendored 改动，须**重建 atomic-rag 镜像**。所有改动带 `# [jonex]` 标记。

### (A) chunk_id 透传（v2 生产检索路径）

**背景**：LightRAG references 透出 `chunk_ids` 后，recalls 的 `chunk_id` 仍恒 null。根因是 v2 生产路径
（REMOTE：kb-service → atomic-rag → raganything）组装 references 时丢弃了 `chunk_ids`（此前只改了
v1 `jonex_core/.../lightrag_adapter.py::LightRAGServerClient.query()`，生产不走）。

| 文件 | 位置 | 说明 |
|---|---|---|
| `raganything/service/task_manager.py` | `_query_via_http`（约 L692-716） | `parse_file_source` 组装 `refs` 的循环中透传 `chunk_ids`：`parsed["chunk_ids"]=chunk_ids; parsed["chunk_id"]=chunk_ids[0]`（本平台 file_source 按 chunk 唯一，取首个） |

### (B) 单片直查 `get_chunk`（按 chunk_id 直连 LightRAG text_chunks）

| 文件 | 位置 | 说明 |
|---|---|---|
| `raganything/service/http_lightrag_client.py` | `get_chunk_by_id` | 新增：`GET /documents/chunks/{chunk_id}`（带 workspace header），返回单片 chunk |
| `raganything/service/task_manager.py` | `get_chunk_by_id` | 新增：调 http_client；`httpx.HTTPStatusError` 404 → None |
| `atomic-rag-server-v2.py` | `@ActionRegistry.register("get_chunk")` | 新增 action handler `handle_get_chunk`；None → 40405 |

### (C) 既有缺陷 TODO：`get_document_chunks`（"查看文档 Chunk 列表"接口）

| 文件 | 位置 | 说明 |
|---|---|---|
| `raganything/service/task_manager.py` | `get_document_chunks` docstring | 打 `# [jonex][TODO]`：该实现对已入库文档恒返回空/结构不符（查 `/documents/paginated` 返回文档级 `{documents}` 而非 text_chunks，键名/层级不符 `{doc_id,total,chunks}` 契约，且 doc_id 语义不匹配——LightRAG 用 `doc-<md5>`、KB id 仅在 file_path 的 `doc=` 锚点）。修复方案见该 TODO 与计划 §9(P11)。单片直查已由 (B) 解决、不受影响 |

**平台侧配套**（不在本目录，登记以便追溯）：

| 文件 | 改动 |
|---|---|
| `jonex_core/capability/atomic/rag/client.py` | `RemoteRAGClient.get_chunk_by_id` 发 action `get_chunk`，40405→None |
| `jonex_core/capability/atomic/rag/lightrag_adapter.py` | v1 兼容路径 `LightRAGServerClient.query()` 解析 `chunk_ids`（生产不走） |
| `capabilities/knowledge_base/services/document_service.py` | `get_chunk` 改直查 `get_chunk_by_id`（不经 get_doc_chunks）+ `doc=` 锚点归属校验 + 清理 `<!--yx:...-->` 标记；`get_document_chunks` 补 TODO |
| `capabilities/knowledge_base/services/search_service.py` | `_rag_fallback_multi` recalls 明细带 `chunk_id`（来自透传） |

升级重放：(A) 保留 `_query_via_http` 的 chunk_ids 透传；(B) 保留 `get_chunk_by_id` 全链路 + action 注册；
(C) get_document_chunks 若重构，按 TODO 用 `doc=` 锚点枚举 text_chunks 返回契约结构。


---

## 二十一、P0 `_converge_delete` 收敛循环（2026-08-06）

> **取代本文件"四、(2) 2-A"与"(4) review-fix"。**
> 线上事故根因：`_run_cleanup` 删一次撞 busy（31s 5 次重试耗光）→ `_poll_old_ids_gone`
> 只 poll 不重删（空等一件从未启动的删除）→ 必然超时 → 文档卡死 60 分钟判 failed。
> 详见 `docs/rag-reparse-strict-cleanup-busy-plan.md`。

### 修复思路

旧 "删一次 → 只 poll" 改为 **"每轮重新发起 delete + 读一致性确认"**：
只要 pipeline 一空闲，下一轮 delete 立刻被受理，不再被 31s 的 busy 重试窗口卡死。

```python
async def _converge_delete(self, task, old_ids):
    deadline = time.monotonic() + RAG_CLEANUP_MAX_ELAPSED  # 默认 1800s
    while time.monotonic() < deadline:
        remaining = old_ids & set(await self._list_doc_ids_by_document(task))
        if not remaining:
            return set()               # 收敛成功
        try:
            await self._http_client.delete_docs(list(remaining), ...)
        except Exception:
            pass                        # busy → 等下一轮重发
        await asyncio.sleep(RAG_CLEANUP_POLL_DELAY)  # 默认 5s
    return remaining                    # 超预算 → 交 KB 3-A 兜底
```

### 改动清单

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | 新增 `_converge_delete(task, old_ids)` | 收敛循环：每轮 `delete_docs` + 查询确认；deadline 按 `per_doc_sec * n` 缩放（`min(1800, max(300, 300 + 2*n))`）；`delete_retry_interval` 控制重发频率避免对 LightRAG 持续轰炸 |
| 同上 | `_reparse_delete_all_old` | 用 `_converge_delete` 替换旧的 `_run_cleanup` + `_poll_old_ids_gone` |
| 同上 | `_resume_cleanup` | 同上（容器重启续删路径） |
| 同上 | ~~`_run_cleanup` / `_poll_old_ids_gone`~~ | **已删除**（共 ~114 行死代码） |
| `raganything/service/http_lightrag_client.py` | `delete_docs` docstring | 更新注释：`_poll_old_ids_gone` → `_converge_delete` |
| `capabilities/knowledge_base/services/document_service.py` | `reparse_document` | **P1 幂等**：同一 doc 已有 in-flight RAG 任务时拒绝重复提交，从源头消除 cleanup ↔ index 竞争 |

### 新增环境变量

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `RAG_CLEANUP_MAX_ELAPSED` | 1800 | 收敛循环最长运行时间（秒） |
| `RAG_CLEANUP_POLL_DELAY` | 5 | 每轮 poll 间隔（秒），默认从旧值 2 提升到 5 |

### 设计要点

- **首次立即发起 delete**（不是等第一轮 poll），`last_delete_attempt = -delete_retry_interval`
- **间隔控制**：`delete_retry_interval = max(poll_delay * 3, 10.0)`，不对同一批持续轰炸
- **进度更新**：每轮将 `remaining` 写入 `task.delete_pending_ids` 供 KB 对账观察
- **fail-open**：delete 失败不抛异常，下一轮重试；查询失败 `continue` 等下一轮
- **超预算**：返回残留集合 → 调用方 `_fail_task` + KB 3-A 动态超时兜底

### KB 侧 P1 幂等（同批次）

`document_service.py` 的 `reparse_document` 入口：若 `doc.rag_task_id` 对应的 RAG 任务
处于 `created/queued/processing`，直接 `ResourceConflictError` 拒绝，不创建第二个任务。
查询失败 fail-open（不阻塞正常 reparse）。

---

## 四、reparse_strict 补偿 cleanup 卡死修复（bugfix，TODO：rag-reparse-strict-cleanup-busy-plan）

> 背景与根因见 `docs/rag-reparse-strict-cleanup-busy-plan.md`。现象：reparse_strict 推送时几个
> chunk 失败 → 严格模式整体失败 → 补偿删除本次全部新 doc（如 258 个）；逐个删撞 LightRAG
> 单 workspace busy 锁 + 每删触发 O(N) KG rebuild，删不完 → 任务卡 `current_step=cleanup`
> → KB 对账 P0-J 守卫无限期 keep ingesting → 文档永久卡死。本次做 1-A/2-A/3-A 三项。

### (1) 1-A：push 阶段终态 failed chunk 纳入有界重推

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage.__init__` | 新增 `self._retry_terminal_failed`（env `RAG_PUSH_RETRY_TERMINAL_FAILED`，默认 true） |
| `raganything/pipeline/stages.py` | `PushChunksStage.execute` per-chunk 重试循环 | track 终态 `failed` 的 chunk 不再立即判永久硬失败，复用 per-chunk 重试预算（`RAG_TRACK_PER_CHUNK_MAX_RETRIES`）与超时 chunk 一起重推，预算耗尽后才落 `terminal_hard_failed` |

要点：降低「几个 chunk 抖动即整体失败 → 全量回滚」的放大；不重复累加计数、尊重 cancel_event、
关闭开关即回退旧行为。

### (2) 2-A：cleanup 整批删除 + 轮询窗口随量缩放 ⚠️ 已被 P0 取代

> **该条目已被 P0 `_converge_delete` 收敛循环取代（2026-08-06）。**
> `_run_cleanup` 和 `_poll_old_ids_gone` 已从 `task_manager.py` 中删除，替换为
> `_converge_delete`：每轮重新发起 `delete_docs` + 读一致性确认，而非"删一次→只 poll"。
> 详见下方 [二十一、P0](#二十一p0-converge_delete-收敛循环2026-08-06)。

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/http_lightrag_client.py` | 新增 `delete_docs(doc_ids: list)` | 一次 DELETE 传全部 doc_ids，LightRAG 单 busy 会话内连删；含 busy 退避重试（`RAG_DELETE_BUSY_RETRIES`/`RAG_DELETE_BUSY_DELAY`） |
| ~~`raganything/service/task_manager.py`~~ | ~~`_run_cleanup`~~ | ~~改为对每个 pending field 调一次 `delete_docs` 整批删除~~ → 已删除，由 `_converge_delete` 取代 |
| ~~`raganything/service/task_manager.py`~~ | ~~`_poll_old_ids_gone`~~ | ~~轮询窗口随 len(old_ids) 缩放~~ → 已删除，由 `_converge_delete` 取代 |

### (3) 3-A 前置：暴露 cleanup 进度

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/models.py` | `TaskInfo.cleanup_total` | 新增字段：进入 cleanup 时的初始待删总量 |
| `raganything/service/task_manager.py` | `_reparse_converge_old` / `_reparse_compensate` | 进入 cleanup 时设置 `task.cleanup_total` |
| `atomic-rag-server-v2.py` | `handle_get_task_status` 返回体 | 新增 `cleanup_total` / `cleanup_pending_count`，供 KB 对账按删除量算动态超时 |

> KB 侧配套改动（不在本文件）：`capabilities/knowledge_base/services/reconciliation_service.py`
> `_handle_failed` 的 cleanup 守卫改为按量动态超时判死
> （`RECONCILE_CLEANUP_BASE_SEC`/`RECONCILE_CLEANUP_PER_DOC_SEC`/`RECONCILE_CLEANUP_CEIL_SEC`）。

### 新增环境变量（保守默认，行为向后兼容）

| 变量 | 默认 | 作用 |
|------|------|------|
| `RAG_PUSH_RETRY_TERMINAL_FAILED` | true | 1-A：终态 failed chunk 是否重推 |
| `RAG_CLEANUP_POLL_PER_DOC_SEC` | 2 | 2-A：cleanup 轮询窗口随删除量缩放系数 |

### (4) review-fix：cleanup poll 超时残留不再乐观置 done/COMPLETED ⚠️ 已被 P0 取代

> **该条目已被 P0 `_converge_delete` 收敛循环取代（2026-08-06）。**
> 旧的 `_run_cleanup` + `_poll_old_ids_gone` 删一次→只 poll 的模式已被彻底移除，
> `_reparse_delete_all_old` 和 `_resume_cleanup` 均改为调用 `_converge_delete`。
> 详见下方 [二十一、P0](#二十一p0-converge_delete-收敛循环2026-08-06)。

<details>
<summary>旧描述（仅供参考）</summary>

> Review 指出：`_run_cleanup` 整批受理即乐观清空 pending，若随后 `_poll_old_ids_gone` 超时仍有
> 旧 doc 残留，原逻辑仍会把 `current_step` 切 `done` 并（调用方）置 COMPLETED → 旧数据残留却暴露
> READY（违反 P0-3 读一致性）。

| 文件 | 位置 | 说明 |
|------|------|------|
| ~~`raganything/service/task_manager.py`~~ | ~~`_poll_old_ids_gone`~~ | 已删除 |
| ~~`raganything/service/task_manager.py`~~ | ~~`_reparse_converge_old`~~ | 已删除 |
| `raganything/service/task_manager.py` | `_execute_pipeline_http` reparse_strict 分支 | P0 后保持：converge 未收敛 → `_fail_task`（保持 cleanup）→ 交 KB 3-A 兜底 |
| `raganything/service/task_manager.py` | `_resume_cleanup` | P0 后保持：续删残留 → `_fail_task`（保持 cleanup），由 `_converge_delete` 引擎驱动 |

效果：旧 doc 未确认删净时任务落 FAILED 且 `current_step=cleanup` 保留 → KB 对账 3-A 动态超时兜底
（据 `cleanup_total` 计时）→ 文档最终 FAILED、前端可见可重传；残留 orphan 交离线/清库处理。

</details>

---

## 十五、P0-1 dup-failed 三态判定（2026-07-29）

> 根因：LightRAG 内容去重产生的 `dup-*` 文档被 track_status 标为 `failed`，
> 但 atomic-rag stages 不区分「真失败」与「dup-failed」，全部计入 hard_failed →
> strict_push 整体失败 → reparse 补偿删全量（如 2/1878 误判触发回滚 809）。
> 修法三件套：1) client 透出 metadata + 新增 doc status 查询能力；
> 2) stages 轮询层识别 dup 并按原件状态三态判定；
> 3) dup_wait 保留轮询而非立即放过。

### 15.1 TrackStatus 透出 metadata + 新增 get_document_status

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/http_lightrag_client.py` | `TrackStatus` dataclass | **新增** `doc_metadata: dict \| None = None` 字段，承载 LightRAG track_status 响应中文档的 `metadata`（含 `is_duplicate`、`original_doc_id`） |
| 同上 | `_parse_track_status()` | 提取 `documents[].metadata`；failed 时填入第一个 failed doc 的 metadata |
| 同上 | 新增 `get_document_status(doc_id, *, tenant_id, kb_id)` | GET `/documents/{doc_id}` → doc info dict（含 `status`）或 None；**依赖 LightRAG vendored 同路径端点**（`Reference/LightRAG/JONEX_CHANGES.md` §10.3）。None 返回附带 WARNING 日志防静默 no-op |
| 同上 | `doc_exists()` | 同样改用 GET `/documents/{doc_id}`（原 HEAD 同一不存在的路径） |

### 15.2 stages 轮询层 dup-failed 三态判定

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | 新增 `_extract_dup_original_id(status)` | 从 `TrackStatus.doc_metadata.is_duplicate`(优先) 或 `error` 文本匹配 "Content already exists" 提取 `original_doc_id` |
| 同上 | 新增 `_query_doc_status(http_client, doc_id, ...)` | 异步查 LightRAG 原件当前状态 → `"processed"` / `"pending"` / `"processing"` / `"failed"` / None |
| 同上 | `PushChunksStage.run()` terminal 收集处 | 三态判定：原件 processed → 良性成功（不计 hard_failed）；原件 pending/processing → **保留在 polling 集合继续轮询**（等待原件完成，靠 per_chunk_timeout 收口）；原件 failed → 落入 hard_failed；原件查不到（None）→ 保守落入 hard_failed 并打 WARNING |

### 15.3 依赖关系

依赖 LightRAG vendored `GET /documents/{doc_id}` 端点。未部署该端点时，`get_document_status` 恒返回 None，dup 三态判定静默退化为 hard_failed（旧行为）。日志中会有 WARNING 提示。


---

## 十六、② push 超时确认前复查真实 doc 状态（治本假 RAG_PUSH_TIMEOUT，2026-07-29）

> 现象（线上 doc 495a5358 f_040_3M_2018_10K.pdf，1878 chunk）：`lightrag_doc_status`
> **processed=2537 / failed=7 / pending=0**（内容其实全跑完），但 PushChunksStage 报
> `RAG_PUSH_TIMEOUT: 15/1878 chunks 轮询超时未确认（1800.0s）` → strict 回滚 530。
>
> 根因：超大文档长尾 chunk 抽取慢，被 `per_chunk_timeout`(900s) 误判超时 → 1-A 重推
> （round1 306 + round2 87）→ 重推内容重复 → 新 track_id 对应 dup，**永不返回 completed**
> → 到全局 `RAG_TRACK_TIMEOUT_SECONDS`(1800s) 仍有 15 个未确认 → strict 假失败回滚。
> **内容其实已 processed，只是 track 确认滞后/重推变 dup。**
>
> 修法：strict 判定 `RAG_PUSH_TIMEOUT` 前，对每个 timed_out chunk **按内容复算 doc_id 直接查
> LightRAG 真实状态**，已 `processed` 则判为已确认（收集 doc_id、移出 timed_out），消除假失败。

### 16.1 改动

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | import | `from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding`（新增后者） |
| 同上 | 新增 `_expected_doc_id(chunk)` | 复算 doc_id：`compute_mdhash_id(sanitize_text_for_encoding(chunk["text"]), prefix="doc-")`，口径与 LightRAG `apipeline_enqueue_documents` 一致（`Reference/LightRAG/lightrag/lightrag.py:1447`） |
| 同上 | `PushChunksStage.execute` §5 分类，`timed_out` 计算后、`confirmed_count` 前 | 新增复查块：`if require_doc_ids and timed_out:` 遍历 timed_out，`_query_doc_status` 查该 doc；`processed` → `ctx.collected_doc_ids.append(did)` + 移出 timed_out；打 INFO 日志「② 超时确认复查——N 个超时 chunk 实际已 processed 判为已确认」 |

### 16.2 要点与依赖

- 复用 §十五 的 `_query_doc_status` + LightRAG vendored `GET /documents/{doc_id}` 端点；端点未部署时
  `_query_doc_status` 恒 None → 不会误确认（保守：仍判超时，退化为旧行为）。
- 仅在 **strict（require_doc_ids）** 且存在 timed_out 时触发；查询量 = 最终 timed_out 数（通常很小）。
- doc_id 复算口径必须与 LightRAG 同版一致（`sanitize_text_for_encoding` + `compute_mdhash_id(prefix="doc-")`），
  升级 LightRAG 时若改了内容规整/哈希，需同步本函数。

### 16.3 验证

- `py_compile Reference/Rag-anything/raganything/pipeline/stages.py` 通过。
- 需重建 `atomic-rag` 镜像生效：`docker compose build atomic-rag && docker compose up -d atomic-rag`。
- 场景：重跑该大文档 → 日志出现「② 超时确认复查——N 个超时 chunk 实际已 processed」，
  strict 不再因假超时回滚，文档最终 READY。

升级重放：改动集中在 `stages.py`，以 `# [jonex] ②` / `_expected_doc_id` 标记。


---

## 十八、raganything 入库计量 contextvar 透传（Gap A，2026-07-30）

> 关联设计：`docs/llm-usage-log-metering-dimension-gaps-fix-plan.md` §4.1、§11.2。
> raganything 直连 llm-gateway 的多模态描述 / summary LLM 与 embedding 调用缺 doc_id/trace_id
> 维度。采用 contextvar 方案：任务入口 set → driver + fallback 闭包每次调用现算注入。
> 所有改动带 `# [jonex]` 标记。

### 18.1 新增 contextvar 模块

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/jonex_metering_ctx.py` | **新增文件** | `set_ingest_ctx(tenant_id, kb_id, doc_id, trace_id)` / `get_ingest_ctx()` / `reset_ingest_ctx(token)` / `build_ingest_headers()` — 参照 LightRAG `jonex_metering.py` 的 contextvar 模式。`build_ingest_headers()` 从 contextvar 构造 X-Jonex-* 头 dict（scene 固定 `raganything_ingest`） |

### 18.2 任务入口 set/reset

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 内 `PipelineCtx` 构造完成后、try 前 | `set_ingest_ctx(tenant_id=task.tenant_id, kb_id=task.kb_id, doc_id=task.document_id, trace_id=task.task_id)` |
| 同上 | `finally` 块最前 | `reset_ingest_ctx(_ingest_token)` |

contextvar 在 `asyncio.create_task` 时复制进子 task，每任务独立 context，并发入库不串租户。

### 18.3 model_factory 计量头改造

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/model_factory.py` | `_build_metering_headers` | 新增 `doc_id` 参数；优先读 `get_ingest_ctx()` contextvar，parameter 作静态兜底；新增 `X-Jonex-Doc-Id` 头 |
| 同上 | `_metered_llm` | `_build_metering_headers()` **移入 `_llm` 闭包内每次现算**（原 build 时烘焙 → 不能随任务变化 doc_id/trace_id） |
| 同上 | `_metered_embedding` | 同上，`_build_metering_headers()` 移入 `_embed` 闭包内每次现算 |

> `_bind_with_overrides` 的 `extra_headers` 保持 build 时 tenant/kb 静态烘焙，per-task
> doc_id/trace_id 由 driver 层 contextvar overlay 覆盖（见 §18.4）。

### 18.4 driver 级 contextvar 叠加（覆盖 registry 主路径）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/models/drivers/openai.py` | `complete()` headers 构造 | lazy-import `build_ingest_headers`，`headers = {..., **spec.extra_headers, **_build_ingest_h()}` — contextvar 覆盖 bake 时静态值 |
| `raganything/models/drivers/anthropic.py` | `_send_request()` headers 构造 | 同上，且补回被遗漏的 `**spec.extra_headers`（此前该 driver 完全没发计量头） |

> **设计要点**：`_bind_with_overrides` 是 registry **主路径**（fallback 只在 bind 失败时用），
> 仅改 fallback 闭包（§18.3）而不改 driver 等于没改。driver 级一处改动同时覆盖 registry + VLM
> +（理论上）所有经 driver 的调用。

### 18.5 生效前提与验证

- **需重建 atomic-rag 镜像**：`docker compose build atomic-rag && up -d`。
- 验证：入库一篇文档 → `metering.llm_usage_log` 的 `scene=raganything_ingest` 行
  `doc_id`/`trace_id` 非空，`request_id` 不再 `auto:`（前缀=task_id）。
- 风险：若 raganything 多模态/summary 调用走线程池而非 `create_task`，contextvar 可能丢失
  → doc/trace 仍空但不报错（回退 static 兜底）。需真链路验证 LLM 与 embedding 行维度齐全。

升级重放：全局搜索 `[jonex]` 定位 `jonex_metering_ctx.py` 整文件、`task_manager.py` 的
`_ingest_token`/`set_ingest_ctx`/`reset_ingest_ctx`、`model_factory.py` 的 `get_ingest_ctx` /
闭包内 `_build_metering_headers`、`drivers/openai.py` `_build_ingest_h` /
`drivers/anthropic.py` `_build_ingest_h`/`spec.extra_headers`。


## 十九、§13.4 纵深防御：动态超时窗口 + 超时不重推在途 chunk（2026-07-29）

> 配合第十六节 ②，从"源头"减少超大文档长尾被误判超时→重推变 dup 的浪费。
> 均为纯防御、向后兼容（floor/仅放大；查状态后才决定重推）。1-B 最小回滚仍列 backlog（改一致性契约，单独评审）。

### 17.1 item1 — 轮询窗口按 chunk 数动态下限（floor）

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | `PushChunksStage.__init__` | 新增 `self._track_scale_per_chunk`(env `RAG_TRACK_SCALE_PER_CHUNK_SEC`,默认 2)、`self._track_scale_ceil`(env `RAG_TRACK_SCALE_CEIL_SEC`,默认 10800) |
| 同上 | `execute` `# ── 4.` 前 | 方法级计算 `eff_track_timeout`/`eff_per_chunk_timeout` = `clamp(base, SCALE×total_chunks, CEIL)`；保持 per-chunk = min(track×0.8, …) < global；`SCALE=0` 关闭回退固定值；放大时打 INFO |
| 同上 | `batch_track_status` 调用 | `max_wait_seconds`/`per_track_timeout_seconds` 改用 `eff_*` |
| 同上 | §5 `RAG_PUSH_TIMEOUT` 文案 | 秒数改用 `eff_track_timeout` |

效果：1878 chunk → track≈3756s(63min)、per_chunk≈3004s（原固定 1800/900 会把长尾误判超时）；小文档（floor 不触发）保持 1800/900 不变。**仅放大不缩小**，不会让任何场景更早超时。

### 17.2 item2 — 超时 chunk 重推前查真实 doc 状态

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/pipeline/stages.py` | `execute` 重推段（`repush_idx` 构建，替换原 `for _t in timeout_tids` 无脑重推） | 对每个 timeout chunk 复算 doc_id 查状态：`processed`→收集 doc_id 判确认（不重推）；`pending/processing/preprocessed`→原 track 重新入 `new_pending_ids` 继续轮询（不重推、不造 dup）；`failed`/查不到→才 repush。打 INFO「已确认 N、继续轮询 M、待重推 K」 |

复用第十六节 `_expected_doc_id` + `_query_doc_status`。与 ②（最终分类兜底确认）互补：item2 在每轮重推前就拦截在途 chunk，避免制造 dup churn；② 在最终判定前再兜一次。

### 17.3 新增环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `RAG_TRACK_SCALE_PER_CHUNK_SEC` | `2` | 每 chunk 追加的轮询窗口秒数；0=关闭（回退固定 `RAG_TRACK_*`） |
| `RAG_TRACK_SCALE_CEIL_SEC` | `10800` | 动态窗口绝对上限秒（3h，< HARD 6h） |

### 17.4 验证

- `py_compile stages.py` 通过；需重建 `atomic-rag` 镜像生效。
- 场景：重跑超大文档 → 日志「§13.4 动态超时窗口 chunks=… track=…s」+「item2 超时复查——已确认/继续轮询/待重推」；
  长尾在放大窗口内首轮确认、重推与 dup 显著减少；strict 不再假超时回滚。

升级重放：改动集中在 `stages.py`，`# [jonex] §13.4` 标记。1-B 最小回滚未实现（backlog）。


## 二十、v2 分阶段耗时埋点（ingest_timing + reconcile_timing 增补，2026-07-30）

> 关联设计：`docs/ingestion-timing-metrics-design.md` §11。
> 补齐 v2 raganything pipeline 三处缺口：push_chunks 永不 close、worker 侧缺 ingest_timing 日志、
> 对账侧缺 pipeline_version 维度。所有改动带 `# [jonex] §11` 标记。

### 20.1 Gap A：push_chunks stage 永远不 close（task_manager.py）

**现象**：`ProgressTrackingCallback.on_document_complete` 先 push "done" 再 `_close_stage()`，
实际关了 "done" 而非 push_chunks。最耗时的推送/抽取段 `ended_at` 恒为 None → `elapsed_seconds` 无值。

**修复**：先 `_close_stage()` 关 push_chunks（由 `on_push_chunks_start` 打开、从未有 `_complete`
来关），再 push "pipeline_done" 再 close "pipeline_done"。管道中更早的 parse/text_insert/multimodal
由各自的 `on_*_complete` 逐一 close，不会被误关。

> **P1-2 改名**：stage key 从 `"done"` 改为 `"pipeline_done"`，避免 timeline 中
> push_chunks → done → ontology_extract 的"done 夹在中间"语义混乱。
> `ProgressDetail.step_name` 仍保留 `"done"`（前端契约不变）。

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | `ProgressTrackingCallback.on_document_complete` | 先 `self._close_stage()` 再 `self._push_stage("pipeline_done")` 再 `self._close_stage()` |

### 20.2 Gap B：ontology 抽取已有 timeline（无需额外改动）

`_run_ontology_extraction` 早在 v1 就已手动 append `StageTiming(stage="ontology_extract")`，
并在 finally 内 close（带 `ended_at` / `elapsed_seconds`）。本节复查确认 Gap B 不存在，无需修改。

### 20.3 v2 ingest_timing 结构化日志（task_manager.py）

新增模块级 helper `_log_ingest_timing_v2(task, status, force_ontology_only, error)`：遍历
`task.timeline` 内白名单 stage 的 `elapsed_seconds`，摊平成 `{stage}_ms` + `worker_total_ms`
双写到 message（供 grep）+ extra（供 JSON 聚合），含 `pipeline_version=v2` 维度。

**P0 缺口 D 兜底 close**：失败/cancel 路径下回调不触发，当前活跃 stage 的 `ended_at` 为 None。
`_log_ingest_timing_v2` 开头遍历 timeline 把所有 `ended_at is None` 的 stage 就地 close（以
`datetime.now(timezone.utc)` 为 ended_at），确保"卡在哪个阶段"数据不丢。

**P1-1 worker_total_ms 口径**：`_QUEUE_KEYS = {"created", "queued"}` 排除排队等待时间，
`worker_total_s` 仅累加非排队 stage，对齐 v1「worker 从取出任务到结束」语义。created_ms /
queued_ms 仍保留在 extra 中供排查。

**P2 error 字段**：`error` 参数传递给 `_log_ingest_timing_v2`，失败/cancel 路径写
`error=getattr(task, "error_message", "")` 或 `error=str(e)[:200]`，成功路径传空字符串。
message 和 extra 均包含此字段。

白名单 `_V2_TIMING_STAGES` = `{created, queued, parse, text_insert, multimodal, push_chunks,
ontology_extract, pipeline_done}`。

调用点覆盖 `_execute_pipeline_http` 全部 4 个终端路径与 `_run_ontology_only` 全部 2 个路径：

| 路径 | status | force_ontology_only | error |
|------|--------|---------------------|-------|
| `_execute_pipeline_http` doc_id 成功 | `completed` | False | (空) |
| `_execute_pipeline_http` doc_id=None | `failed` | False | `task.error_message` |
| `_execute_pipeline_http` except TaskCancelledError | `cancelled` | False | `"task_cancelled"` |
| `_execute_pipeline_http` except Exception | `failed` | False | `str(e)[:200]` |
| `_run_ontology_only` 成功 | `completed` | True | (空) |
| `_run_ontology_only` CONFIG_INVALID | `failed` | True | `task.error_message` |

失败路径在 re-raise 前打日志：P0 兜底 close 保证未关闭 stage 有 ended_at → elapsed_seconds 非零；
P1-1 口径只计 worker 内耗时；P2 error 字段直写 message + extra。遵循 §3.4 A 方案甲双写约定。

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | 模块级 `_V2_TIMING_STAGES` + `_log_ingest_timing_v2()` | **新增**：v2 timeline → ingest_timing 日志摊平 helper |
| 同上 | `_execute_pipeline_http` success/failure/cancel/exception | 各路径插 `_log_ingest_timing_v2` |
| 同上 | `_run_ontology_only` success/CONFIG_INVALID | 各路径插 `_log_ingest_timing_v2(force_ontology_only=True)` |

### 20.4 对账侧 v2 兼容（reconciliation_service.py）

| 文件 | 位置 | 说明 |
|------|------|------|
| `capabilities/knowledge_base/services/reconciliation_service.py` | 模块级 `_V2_STAGE_KEYS` | **新增**：v2 stage 白名单 `{created,queued,parse,text_insert,multimodal,push_chunks,ontology_extract,pipeline_done}` — `_normalize_stage_timings` v2 list 形态仅保留白名单内 stage，其余静默跳过 |
| 同上 | 模块级 `_QUEUE_KEYS` | **新增**：`{created, queued}` — `_normalize_stage_timings` v2 分支排除排队键不算入 `worker_total_ms`（P1-1，与 task_manager 口径一致） |
| 同上 | `_normalize_stage_timings` v2 分支 | 加入白名单过滤 + 排队键排除 |
| 同上 | 模块级 `_detect_pipeline_version()` | **新增**：list → `v2`，dict → `v1`，None/empty → None |
| 同上 | `_handle_completed` reconcile_timing 日志 | 新增 `pipeline_version` 字段（message 内嵌 + extra 结构化），便于大盘按管道版本聚合 |

### 20.5 验证

- `py_compile` 所有改动文件通过。
- 需重建 `atomic-rag` 镜像并重启 knowledge-base-service 生效：
  ```bash
  docker compose build atomic-rag && docker compose up -d atomic-rag knowledge-base-service
  ```
- 验证要点（见设计 §11.6）：
  - 入库一篇文档 → `docker logs jonex-atomic-rag | findstr ingest_timing` 出现
    `pipeline_version=v2` 且 `push_chunks_ms` / `ontology_extract_ms` 非空
  - `reconcile_timing` 日志含 `pipeline_version=v2` 及各阶段耗时
  - 失败场景：`status=failed` 仍含已完成阶段耗时

### 新增环境变量（無）

复用已有 `INGEST_TIMING_ENABLED`（`jonex_core/common/timing.py`），`_log_ingest_timing_v2`
内部调用 `timing_enabled()` 检查。关闭 → 不产生任何 timing 日志，行为向后兼容。

升级重放：全局搜索 `# [jonex] §11` 定位 `task_manager.py` 的 Gap A 修复 + `_log_ingest_timing_v2`
+ 6 个调用点，以及 `reconciliation_service.py` 的 `_V2_STAGE_KEYS` / `_detect_pipeline_version`
+ `_handle_completed` 的 `pipeline_version` 字段。

---

## [jonex] OpenKB 链路：解析产物落共享卷 + task status 暴露路径

> 目的：让 knowledge_base 的 `pipeline_type=openkb` 分支能拿到「解析出的 markdown + 图片」，交给 OpenKB 容器编译。解析产物原本只落在容器本地 `parser_output_dir`（jonex-rag-storage），openkb 读不到；这里额外把产物写到共享卷 `jonex-rag-inputs`（atomic-rag 挂 `/app/inputs`，openkb 挂 `/app/data/inputs`，同卷不同挂载点）。

改动点（均标 `# [jonex]`）：

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | 新增方法 `TaskManager._write_openkb_artifact(task, ctx)` | 把 `ctx.content_list`（MinerU blocks，img_path 已是绝对路径）序列化为 `content.md`（text/table/equation/image），图片复制到 `assets/`，写到 `{RAG_INPUTS_DIR:-/app/inputs}/parsed/{document_id}/`；相对路径 `parsed/{document_id}/content.md` + `assets` 记入 `task.result_summary.extensions` 与 `task.storage`。best-effort，异常不影响主链路；`RAG_OPENKB_ARTIFACT_ENABLED`（默认 on）可关。 |
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 内 `task.result_summary = summary` 之后 | 调用 `self._write_openkb_artifact(task, ctx)`。 |
| `atomic-rag-server-v2.py` | `handle_get_task_status` 返回 data | 顶层新增 `parsed_markdown_path` / `assets_dir`（取自 `task.result_summary.extensions`），供 KB 对账读取。 |

新增环境变量：

- `RAG_INPUTS_DIR`（默认 `/app/inputs`）：共享 inputs 卷在 atomic-rag 容器内的挂载点，OpenKB 产物写此目录下 `parsed/{document_id}/`。
- `RAG_OPENKB_ARTIFACT_ENABLED`（默认 `true`）：总开关。

消费侧（非本仓）：`capabilities/knowledge_base/services/reconciliation_service.py::_dispatch_openkb_compile` 读 task status 的 `parsed_markdown_path`/`assets_dir`，换成相对 inputs 卷根路径后交 `atomic.openkb.v1 compile_parsed_document`；OpenKB adapter 用 `OPENKB_INPUT_ROOT`（`/app/data/inputs`）解析该相对路径读到同一份文件。

### 追加：`execution_mode=parse_only`（OpenKB KB 级互斥）

为让 `pipeline_type=openkb` 的文档「只解析、不写 LightRAG/Neo4j」，新增 parse-only 执行模式：

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | `_execute_pipeline_http` 分支 | `task.execution_mode == "parse_only"` → 调 `_run_parse_only`，跳过 multimodal/push_chunks/ontology。 |
| `raganything/service/task_manager.py` | 新增 `_run_parse_only(task, handle)` | 调 `self._pipeline_executor.parse_document(...)` 得 content_list（复用解析缓存），写 `_write_openkb_artifact`，置任务 COMPLETED。不入库、不抽本体。 |

`execution_mode` 已由 `handle_insert`(atomic-rag-server-v2.py L361) → `CreateTaskRequest` → `task.execution_mode` 原样透传，无需改 server。

消费侧（非本仓）：`knowledge_base/services/document_service.py` 对 `pipeline_type=openkb` 文档以 `execution_mode="parse_only"` 调 `insert`；`client.py`(Remote/Local/Mock/abstract) 与 `lightrag_adapter_v2.insert` 已加 `execution_mode` 透传；`reconciliation_service._handle_completed` 对 openkb 文档标 `ontology_status=READY`，避免本体重试循环。

### 修复：handle_insert 透传 execution_mode

`atomic-rag-server-v2.py::handle_insert` 的 `CreateTaskRequest` 此前**未透传 `execution_mode`**（仅 retry handler 透传），导致 `pipeline_type=openkb` 文档虽由 knowledge_base 传了 `execution_mode=parse_only`，到 atomic-rag 后仍按 `full` 执行（照常入 LightRAG + 抽本体）。已补 `execution_mode=params.get("execution_mode", "full") or "full"`，使 parse_only 生效（只解析、落 OpenKB 产物、跳过入库/本体）。

> 相关无关联但同批定位的部署缺陷（不在本仓）：
> - `deploy/docker/openkb-source.Dockerfile`：capability 依赖此前经 `uv pip install`（UV_SYSTEM_PYTHON）落到 `/usr/local`，运行时用 `/app/.venv` → sqlalchemy 等缺失致 openkb 容器崩溃循环。改为 `uv pip install --python /app/.venv/bin/python` 并补 sqlalchemy/greenlet/asyncpg/neo4j/bcrypt。
> - `jonex_core/capability/locator.py::_normalize_atomic_id`：0 点短名（`openkb`）漏 `.v1`，致 `get_openkb_client("atomic.openkb.v1")` 回退 LOCAL。改 `count(".") <= 1` 统一补 `.v1`。

### 补充：OpenKB 产物图片引用路径对齐

`_write_openkb_artifact` 序列化 image 块时,markdown 引用从 `assets/<name>` 改为 `images/<document_id>/<name>`。原因:knowledge_base 的 openkb adapter 会把 `assets_dir` 复制到 `wiki/sources/images/<document_id>/`,而 source md 位于 `wiki/sources/<document_id>.md`,故相对引用须为 `images/<document_id>/<name>` 才能在 OpenKB Wiki 中正确显示(符合 OpenKB 短文档图片约定)。adapter 侧同步移除了「图片引用未重写」的 warning。
## §12 `2026-08-03` — 多模态 chunk 页码缺失修复（方案⑧ 配套）

### 背景

MinerU 解析产出中，图片 item 的 `page_idx` 在顶层 dict（与文本 item 同源），
但 `separate_content` 将 item 原样放入 `multimodal_items[].original`，只创建了空 `item_info={}`。
`_collect_multimodal_chunks` / VLM 上下文窗口从 `item_info.get("page_idx")` 取 → `None` →
`_build_file_source` 不写 `page=` → `to_location` 退化为 `type=chunk`（无页码）。
同一份文档的文本 chunk 正确拿到了 `page=11`，证明 MinerU 本身产出了页码；只是多模态链路读错了字段。

### 改动

**`raganything/pipeline/stages.py`** — `_collect_multimodal_chunks` L1653
- `page = item_info.get("page_idx")` 之后加 `original.get("page_idx")` 兜底

**`raganything/modalprocessors.py`** — VLM 上下文窗口 `current_page` L185
- 同步加 `original.get("page_idx")` 兜底

### 影响

修复前：PDF 图片引用一律退化为 `type=chunk`（无页码）。
修复后：图片引用与文本引用同源，`type=page` + `page_no` 正确，前端可跳转页码。

### 重建

改动在 vendored `Reference/Rag-anything/**` → 需重建 `atomic-rag` 镜像 + 重抽目标 KB。

升级重放：全局搜索 `# [jonex] §12` 定位两处 `page_idx` 兜底。


## §13 `2026-08-10` — COS 文件存在性校验 + error_code 透传 + 失败日志（P0-1 / P0-a）

### 背景

线上文档 `0d1fe634` 的 task 创建瞬间失败 `FILE_NOT_FOUND`：
`_validate_and_enqueue` 对所有任务做 `os.path.exists(task.file_path)`，但 COS 后端的
`file_path` 是 storage_key 标识符而非本地路径 → 必返回 False → 误判 FILE_NOT_FOUND。

同时 `_fail_task` 只写 task 字段不写日志 → KB 对账 `_finalize_failure` 读 `status_info.error`
(`task.error_message`) 时可能拿到空值 → 生成误导性「任务确认丢失」文案掩盖真实错误。

此外 `get_task_status` 响应不包含 `error_code` 字段 → KB 侧 P1-5 确定性终态短路
（FILE_NOT_FOUND / OBJECT_FETCH_FAILED 直接落 FAILED 跳过 3 拍确认）永远不触发。

### 改动

| 文件 | 位置 | 说明 |
|------|------|------|
| `raganything/service/task_manager.py` | `_validate_and_enqueue` L1100 | `# [jonex] P0-1`：COS 后端（`storage_backend=="cos"` 且 `storage_key` 非空）跳过 `os.path.exists` 本地路径检查，文件由 pipeline 阶段从 COS 按需下载；非 COS 仍走旧逻辑 |
| 同上 | `_fail_task` L2240 | `# [jonex] P0-1`：补 `logger.error`（含 task_id / error_code / message），防止 KB 对账拿不到真实错误文案 |
| `atomic-rag-server-v2.py` | `handle_get_task_status` L299 | `# [jonex] P0-a`：响应 `data` 新增 `"error_code": task.error_code.value if task.error_code else None`，供 KB 侧 P1-5 确定性终态短路使用 |

### KB 侧配套（不在本目录，登记以便追溯）

| 文件 | 改动 |
|------|------|
| `capabilities/knowledge_base/services/reconciliation_service.py` | `_handle_failed` P1-5：`error_code` 匹配 + `error_msg` 文本前缀兜底确定性终态直接落 FAILED |
| 同上 | P0-2/P0-3/P0-新/P0-b/P1-c/P1-d/P1-4 `death_verdict` 全链路整改 |

### 重建

需重建 `atomic-rag` 镜像：`docker compose build atomic-rag && docker compose up -d atomic-rag`。

升级重放：全局搜索 `# [jonex] P0-1` / `# [jonex] P0-a` 定位三处改动。
