-- ============================================================================
-- lightrag_doc_chunks 增加 (workspace, doc 锚点, chunk_order_index) 表达式索引
--
-- 背景
--   get_chunks_by_doc_id()（Reference/LightRAG/lightrag/kg/postgres_impl.py）
--   以 `WHERE workspace=$1 AND file_path LIKE '%doc=<uuid>%'` 枚举某文档的全部
--   文本 chunk。前导通配让 btree 失效；测试环境实测该查询为 Parallel Seq Scan
--   （lightrag_doc_chunks 约 12 万行 / 232MB，单次 135ms，Sort 落盘 19MB）。
--
-- 方案（2026-11-20 评审后调整）
--   不再新增生成列，改为**表达式索引**。file_path 形如
--   `kb=<kb>|doc=<平台uuid>|tenant=...|chunk=N|...`（另有 file=/page=/ctype= 等
--   可选段），用 substring(file_path from 'doc=([^|]+)') 抽出平台文档 uuid。
--   PG 15 的 `substring(text, text)` 由 textregexsubstr 实现，pg_proc.provolatile
--   = 'i'（immutable，已在 15.19 上实测），可以进表达式索引。
--
--   与生成列方案的差别：
--     · 不 ADD COLUMN ⇒ 不重写 heap、不重建既有索引、不持 ACCESS EXCLUSIVE；
--       只有一次在线索引构建（CREATE INDEX CONCURRENTLY 不阻塞读写）。
--     · 不存在「全新库建表时没有该列、而本单元已登记为已应用」的列漂移问题；
--       缺索引最坏只是慢，不会报 column does not exist。
--   第三、四列 chunk_order_index, id 让分页查询走有序 Index Scan 提前停机、免掉 Sort：
--   显式 limit 的排序键是 `chunk_order_index, id`，三列索引会因缺 id 仍要一次 Sort
--   （实测确认），四列才能完全匹配 ORDER BY。
--
-- 查询侧（同批代码已改）
--   WHERE workspace=$1 AND substring(file_path from 'doc=([^|]+)') = $2
--   纯等值，不加 OR。OR 会让两个分支都必须能走同一索引，LIKE 分支走不了，
--   优化器只能把 doc 条件退化成 Filter（实测确认），等于白建索引。
--   等值与旧的 LIKE 在真实数据上结果集一致：kb_doc_id IS NULL 时 LIKE 必然为假
--   （能命中 doc=<非空id> 的路径其 doc= 后必接非 '|' 字符，substring 必非 NULL），
--   所以「无锚点需保留 LIKE 回落」的担心在逻辑上不成立。等值还顺带修掉一个
--   潜在误命中：LIKE '%doc=abc%' 会命中 doc=abcd 的文档，等值不会。
--
-- 幂等与兼容
--   列与索引均 IF [NOT] EXISTS。本单元同时清理早期版本留下的生成列
--   kb_doc_id 与索引 idx_lightrag_doc_chunks_workspace_kb_doc —— 对从未执行过
--   早期版本的库（如尚未执行的测试环境），这两步是 no-op。
--   本文件**不做全局事务包裹**：CREATE INDEX CONCURRENTLY 与
--   DROP INDEX CONCURRENTLY 都不允许在事务块内执行。
--
-- 与 LightRAG 自身 check_tables() 的交互
--   check_tables() 只在表不存在时 CREATE TABLE，之后仅补 idx_<table>_id 与
--   idx_<table>_workspace_id，不会 DROP 未知索引，因此本增量与它不冲突。
--   反向有一条要记住：lightrag_* 表由 LightRAG 服务启动时创建，全新库 / 新数据卷
--   上 apply.sh 可能在这张表出现之前就跑完本单元 —— 此时本文件只发 NOTICE 并正常
--   结束，apply.sh 会把它登记为「已应用」，之后不会自动重跑。**LightRAG 首次启动
--   建表之后，请补跑一次**：
--     bash deploy/postgres/update/apply.sh \
--       --only 20261120184137_lightrag_doc_chunks_doc_anchor_index.sql --yes
--
-- 回退
--   DROP INDEX CONCURRENTLY IF EXISTS public.idx_lightrag_doc_chunks_ws_doc_ord;
--   （纯索引回退，不重写表。）
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1) 清理早期生成列方案（幂等）。表不存在时整体跳过。
--    DROP INDEX CONCURRENTLY 在事务块内不可执行，故单独放在 DO 块外面。
-- ----------------------------------------------------------------------------
SELECT format('DROP INDEX CONCURRENTLY IF EXISTS %I', 'idx_lightrag_doc_chunks_workspace_kb_doc')
WHERE to_regclass('public.lightrag_doc_chunks') IS NOT NULL
\gexec


DO $$
BEGIN
    IF to_regclass('public.lightrag_doc_chunks') IS NULL THEN
        RAISE NOTICE 'public.lightrag_doc_chunks 不存在（LightRAG 尚未建表），跳过本单元；服务首次启动建表后请用 --only 补跑';
        RETURN;
    END IF;

    -- 老生成列会随 DROP COLUMN 触发整表重写；只在列确实存在时才做。
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name   = 'lightrag_doc_chunks'
           AND column_name  = 'kb_doc_id'
    ) THEN
        ALTER TABLE public.lightrag_doc_chunks DROP COLUMN kb_doc_id;
        RAISE NOTICE '已删除早期生成列 kb_doc_id（本次随表重写）';
    END IF;
END
$$;


-- ----------------------------------------------------------------------------
-- 2) 表达式索引：CONCURRENTLY 不能进事务块，用 \gexec 在顶层按需执行。
--    表达式必须与查询侧逐字一致，否则规划器不会用它。
-- ----------------------------------------------------------------------------
-- 第四列 id 是必需的：显式 limit 的查询按 `ORDER BY chunk_order_index, id` 排序，
-- 只有 `(workspace, 锚点, chunk_order_index)` 三列时索引只能提供前两者的有序性，
-- 仍要一次 Sort。带上 id 后整条 ORDER BY 都能从索引得到，LIMIT 才能真正提前停机。
SELECT format(
           'CREATE INDEX CONCURRENTLY IF NOT EXISTS %I ON public.lightrag_doc_chunks '
           '(workspace, (substring(file_path from ''doc=([^|]+)'')), chunk_order_index, id)',
           'idx_lightrag_doc_chunks_ws_doc_ord'
       )
WHERE to_regclass('public.lightrag_doc_chunks') IS NOT NULL
\gexec


-- ----------------------------------------------------------------------------
-- 3) 统计信息：pg_stat_user_tables.n_live_tup 严重滞后（实测 386 vs 实际
--    12 万），不 ANALYZE 规划器可能仍按旧基数选 Seq Scan，索引形同虚设。
-- ----------------------------------------------------------------------------
SELECT 'ANALYZE public.lightrag_doc_chunks'
WHERE to_regclass('public.lightrag_doc_chunks') IS NOT NULL
\gexec


-- ============================================================================
-- 验证（迁移前后人工执行，均为只读；<uuid>/<workspace> 换成真实值）
-- ----------------------------------------------------------------------------
-- 迁移后（期望 Index Scan / Bitmap Index Scan，只扫该文档的 chunk，且不限 Sort）：
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT id FROM public.lightrag_doc_chunks
--    WHERE workspace = '<workspace>'
--      AND substring(file_path from 'doc=([^|]+)') = '<uuid>'
--    ORDER BY chunk_order_index;
--
-- 带 LIMIT 时走有序索引、无 Sort 节点：
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT id FROM public.lightrag_doc_chunks
--    WHERE workspace = '<workspace>'
--      AND substring(file_path from 'doc=([^|]+)') = '<uuid>'
--    ORDER BY chunk_order_index, id
--    LIMIT 100;
--
-- 索引存在且有效（indisvalid 必须为 t；若为 f 说明 CONCURRENTLY 中断留下了
-- 无效索引，IF NOT EXISTS 不会自动重建，需先 DROP INDEX CONCURRENTLY 再重跑）：
--   SELECT indexrelid::regclass, indisvalid, pg_get_indexdef(indexrelid)
--     FROM pg_index
--    WHERE indrelid = 'public.lightrag_doc_chunks'::regclass;
--
-- 锚点分布（no_anchor 大于 0 时需确认这些行是否本应命中）：
--   SELECT count(*) AS total,
--          count(*) FILTER (WHERE substring(file_path from 'doc=([^|]+)') IS NULL) AS no_anchor,
--          count(DISTINCT substring(file_path from 'doc=([^|]+)')) AS docs
--     FROM public.lightrag_doc_chunks;
-- ============================================================================
