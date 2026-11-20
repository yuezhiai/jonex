# 本地补丁清单（LightRAG）

本目录是 vendored 的 LightRAG 源码。以下补丁由本仓库维护，**上游升级时必须逐条重新应用，
或确认上游已吸收**，否则对应缺陷会静默回归。补丁处均带 `# [jonex]` 注释标记。

升级后在**仓库根目录**自检（测试放在根 `tests/unit/`，不要在 `Reference/LightRAG/` 下跑）：

```bash
uv run --with numpy --with tenacity --with pipmaster --with tiktoken --with pgvector \
  pytest tests/unit/test_lightrag_pg_pool_reconnect.py \
         tests/unit/test_lightrag_retry_policy.py \
         tests/unit/test_lightrag_health_pg_probe.py \
         tests/unit/test_lightrag_chunks_limit.py -q
```

`tests/` 被根 `.gitignore` 忽略；新增守护测试要 `git add -f`，否则 CI 拿不到。

## 1. PostgreSQL 连接池重建路径自死锁 + 超时兜底

- **文件**：`lightrag/kg/postgres_impl.py`
- **位置**：`PostgreSQLDB._ensure_pool` / `_discard_pool` / `_reset_pool` / `_before_sleep` / `_run_with_retry` / `initdb`
- **改动**：
  - `_ensure_pool` 改为建池单飞：锁只负责选出唯一建池者，等待移到锁外，并加 `pool_init_timeout` 兜底
  - 新增无锁 `_discard_pool`；`_before_sleep` 改调它；`_reset_pool` 保留为兼容入口
  - `pool.acquire()` 带 `pool_acquire_timeout`
  - 连接参数支持 `command_timeout`
  - `_run_with_retry` 改为**手动 acquire/release**，`release` 传独立 `pool_release_timeout`（默认 1s）
    并在 `finally` 里吞掉 release 自身的超时。原因：`asyncpg.Pool.release` 在未显式传 timeout 时
    沿用 acquire 时的 timeout 做取消握手（外层还有 `asyncio.shield`），一次 30s 语句超时实际
    耗时会变成约 60s（实测 60.25s）。手动 release 后压到 1× + ≤1s。
  - 新增配置项：`POSTGRES_POOL_ACQUIRE_TIMEOUT`(30s) / `POSTGRES_POOL_INIT_TIMEOUT`(120s) /
    `POSTGRES_COMMAND_TIMEOUT`(默认不限) / `pool_release_timeout`(1s，暂无 env，走代码默认)
- **原因**：`_ensure_pool` 原实现在锁内执行整个 `initdb()`，而 initdb 的 tenacity `before_sleep`
  回调会再次申请同一把不可重入的 `asyncio.Lock`。连接池被置空后首轮重建失败
  （数据库正在恢复、磁盘满导致连接被拒）即永久死锁：不抛错、不超时、无日志，只能重启进程。
  此外 `pool.acquire()` 与单条语句均无超时上限，池耗尽或慢查询会放大为无界等待。
- **守护测试**：`tests/unit/test_lightrag_pg_pool_reconnect.py`
- **回归场景**：停掉 PostgreSQL 再恢复，lightrag 不重启即可恢复响应。

## 2. `/health` 增加依赖探针（PG / Milvus / Neo4j → 503）

- **文件**：`lightrag/api/health_probe.py`（新增）、`lightrag/api/lightrag_server.py`
- **位置**：`get_status()` 返回前；新模块导出 `probe_postgres` / `probe_dependencies` /
  `get_dependencies` / `apply_probe`
- **改动**：
  - `probe_postgres` 用**一次性 `asyncpg.connect`**（参数从 `db` 对象取，SSL 分支与 `initdb` 一致），
    不走共享池。原因：共享池的 `acquire(timeout)` 报告的是本进程池压力而非依赖可达性 ——
    重度入库打满 25 条连接时探针会假 503；且归还连接还要再等一轮取消握手，1s 探针实际约 2s。
  - Milvus / Neo4j 用 TCP 可达性探测（`MILVUS_URI` / `NEO4J_URI` 解析 host:port），
    只探**实际配置**的后端；未配置的返回 `skipped` 且不降级（否则切到 JSON 开发档会永久 503）。
  - 三依赖并发探测（`asyncio.gather`），单项 1s，总预算约 2s，远低于 compose `timeout: 10s`。
  - 结果带 `ms` 耗时，形状为 `dependencies: {postgres: {state, ms}}`。
  - 结果缓存 + 单飞锁：up 5s / down 2s，任意并发下每 TTL 最多一次连接。
  - 任何 degraded 状态（`unavailable`/`not_initialized`/`error`）返回 `503` + `status="degraded"`；
    `up` / `skipped` 保持 `200`，原有字段逐字不变，仅新增 `dependencies`。
- **原因**：`/health` 原先只读进程内状态，不碰 PG / Milvus / Neo4j，数据层全挂仍返回 200。
  2026-11-19 数据层故障期间 PG 恢复约 40 分钟，compose healthcheck 与运维巡检全程未发现，
  只能人工重启。compose healthcheck 只看状态码，所以必须 503，不能 200 + degraded。
- **守护测试**：`tests/unit/test_lightrag_health_pg_probe.py`
- **回归场景**：停掉 PostgreSQL 后 `/health` 在 ≤2s 返回 503；恢复后回到 200。
  仍开着 JSON 存储档时 `/health` 不应因 PG 缺失而 503（`skipped`）。

## 3. 超时不得被判为可重试（`retry_policy`）

- **文件**：`lightrag/kg/retry_policy.py`（新增）、`lightrag/kg/postgres_impl.py`
- **位置**：`postgres_impl.py` 模块级断言 + `PostgreSQLDB.__init__` 的 `self._retry_predicate`
  + `initdb` / `_run_with_retry` 两处 `AsyncRetrying(retry=...)` + `_before_sleep`
- **改动**：
  - 新增 `retry_policy.is_timeout()` / `is_retryable()`：超时**先短路返回不可重试**，
    再判断连接类异常白名单。不能靠「从元组里删掉 `TimeoutError`」——Python 3.10+ 的
    `asyncio.TimeoutError` 就是内建 `TimeoutError`，而后者又是 `OSError` 子类，
    元组里留着 `OSError` 就仍会被 `isinstance` 匹配。
  - `_transient_exceptions` 移除 `asyncio.TimeoutError` / `TimeoutError`
  - `AsyncRetrying` 的 `retry=` 改用 `retry_if_exception(lambda exc: is_retryable(...))`
  - `_before_sleep` 对超时只记日志、不拆池（**防御分支，正常不可达**：判定已短路）
  - 模块级 `if is_retryable(TimeoutError()): raise RuntimeError(...)` —— 上游把 `retry=` 改回去、
    或 `retry_policy` 被覆盖时，服务**启动即失败**，而不是等某次 PG 挂死才发现 10 次重试 + 9 次拆池
- **原因**：原实现把超时当瞬时故障重试，且每次重试前丢弃整个连接池。一次 60s 超时会被
  放大成「多次尝试 + 多次拆池」，快速失败变成慢速重试 + 池抖动。
- **守护测试**：`tests/unit/test_lightrag_retry_policy.py`
- **回归场景**：语句超时后日志只有超时错误，没有 `transient connection issue` 重试行，
  池也不被丢弃。

## 4. 启动期 DDL 豁免交互超时

- **文件**：`lightrag/kg/postgres_impl.py`
- **位置**：`check_tables()`（包装 `_check_tables_impl`）、`_run_with_retry`、
  `query` / `execute` 的 `_operation`、`_create_pagination_indexes`
- **改动**：
  - 新增实例标记 `self._startup_ddl`；`check_tables()` 在 try/finally 里置位/复位
  - 置位期间：连接上临时 `SET statement_timeout = 0`（归还时 `RESET ALL` 复原）；
    `query`/`execute` 单次调用传 `NO_TIMEOUT` 绕开 `command_timeout`
  - `_create_pagination_indexes` 改查 `pg_index.indisvalid`：`CREATE INDEX CONCURRENTLY`
    被取消会留下 INVALID 索引，而 `IF NOT EXISTS` 看到同名索引就跳过、**永不重建**；
    现在先 `DROP INDEX CONCURRENTLY` 再建，`except` 放宽到 `Exception`
- **原因**：`command_timeout` 作用于池里所有连接，包括启动期 `ALTER COLUMN TYPE` /
  `CREATE INDEX CONCURRENTLY`。大表上超过 30s 会留 INVALID 索引或让 ALTER 静默回滚
  （异常被 `logger.warning` 吞），表现为「迁移没做但服务正常起来」。
- **守护测试**：无独立单测（依赖真实 DDL 时序）；验证方式是启动日志无 CIC 取消、
  `SELECT indisvalid FROM pg_index` 全为 `t`。
- **回归场景**：在缺分页索引的大表上启动，索引能建成且 `indisvalid = t`。

## 5. `get_chunks_by_doc_id` 按 `doc=` 锚点等值查询 + 可选 limit

- **文件**：`lightrag/kg/postgres_impl.py`（SQL 模板 `get_chunks_by_doc_id`）、
  `lightrag/api/routers/document_routes.py`
- **位置**：`SQL_TEMPLATES["get_chunks_by_doc_id"]`、`PGDocStatusStorage.get_chunks_by_doc_id`、
  `list_document_chunks`
- **改动**：
  - 查询从 `file_path LIKE '%doc=<uuid>%'` 改为
    `substring(file_path from 'doc=([^|]+)') = $2`（纯等值，不加 OR）
  - `limit` 可选：默认 `None` 全量（与旧 SQL 逐字一致）；显式时加 `, id` 排序键与 `LIMIT $3`
  - 路由 `GET /documents/chunks` 增加可选 `limit` query 参数
- **配套迁移**：`deploy/postgres/update/20261120184137_lightrag_doc_chunks_doc_anchor_index.sql`
  建表达式索引 `(workspace, substring(...), chunk_order_index)`。表达式必须与查询逐字一致。
- **原因**：前导通配用不上 btree，12 万行表上只能全表扫。等值不丢数据：无锚点的行
  `substring` 为 NULL，等值与旧 LIKE 都不会命中；等值还修掉 `LIKE '%doc=abc%'` 误命中
  `doc=abcd` 的潜在问题。不加 OR 是因为 OR 两支必须能走同一索引，LIKE 支走不了，
  优化器会把 doc 条件退化成 Filter。
- **守护测试**：`tests/unit/test_lightrag_chunks_limit.py`
- **回归场景**：`EXPLAIN` 走 `idx_lightrag_doc_chunks_ws_doc_ord`；带 `LIMIT` 时无 Sort。
