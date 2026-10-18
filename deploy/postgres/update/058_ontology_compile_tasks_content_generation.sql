-- ============================================================
-- 058_ontology_compile_tasks_content_generation.sql
-- 编译任务记录**提交时的文档代次**（content_generation，fencing）
--
-- 背景（P2「本体编译独立容器」）
--   KB 对账收尾（`reconciliation_service._handle_completed`）第一件事是**代次栅栏**：
--   `task_generation = status_info.get("content_generation", 0)` 与
--   `doc.content_generation` 比，`task < doc` 判「旧代次的结果」——此时若接管行在途
--   （编译步正在跑时恒在途）就 `return "skipped"`，**文档停在 EXTRACTING、永不 READY**。
--   056 建表时漏了这一列 ⇒ 新服务恒回「0 代次」⇒ 凡是被**重新解析过**的文档
--   （`content_generation >= 1`）走新服务必然命中该栅栏。全新上传的文档（gen=0）
--   看不出来 —— 灰度若只传新文档会漏掉它。
--
-- 语义
--   `compile` 提交时由调用方（KB 侧 `OntologyService.retry_extract`）随参数带上，
--   落进任务行；`get_compile_status` 原样回显，与 rag 链
--   （`retry_ontology_extract(content_generation=…)`）同口径。
--   **不能**在读取侧现取 `doc.content_generation` 顶上：那会让「文档在编译期间又被
--   重解析」的陈旧结果也判成同代、被照常应用。代次必须冻结在提交那一刻。
--   0 = 「未提供」（与 rag 链的缺省同值）；存量行全部 0 = 与改造前逐字一致。
--
-- 为什么只写 update/、不并回 056
--   `deploy/postgres/update/*.sql` 是**增量 DDL、按序人工触发**，且带版本表
--   （`public.schema_migrations`）跳过已应用者。056 若已在某个库里跑过，**改它对
--   存量库静默不生效** —— 补列只能新开一个文件。
--
-- 发版次序：**先 `make db-migrate`，再换 ontology-compile 新镜像** ——
--   反序时 `CompileTaskStore.save()` 会因 `_COLUMNS` 含该列而撞上不存在的列，
--   当场 `UndefinedColumn` 硬失败（每条 compile 入队都失败）。
--
-- 回滚：本列是**新增**的、且读侧对缺键/0 都有定义（栅栏只在 `task < doc` 时生效），
--   回滚镜像即可；要一并撤列再跑
--   `ALTER TABLE ontology_compile.compile_tasks DROP COLUMN IF EXISTS content_generation;`
--   （撤列后**回滚到旧镜像**，否则旧镜像是 056 形状、不含该列，行为正常）。
--
-- 幂等: ADD COLUMN IF NOT EXISTS / COMMENT 重复执行均为空操作。
-- ============================================================

BEGIN;

-- 058：编译任务的提交代次。类型与 `knowledge_base.knowledge_document.content_generation`
-- 对齐（Integer）；NOT NULL DEFAULT 0 让存量行即刻可读（0 = 与改造前同义）。
ALTER TABLE ontology_compile.compile_tasks
    ADD COLUMN IF NOT EXISTS content_generation INTEGER NOT NULL DEFAULT 0;
COMMENT ON COLUMN ontology_compile.compile_tasks.content_generation IS
    'compile 提交时的文档代次（fencing）：KB 对账代次栅栏与 doc.content_generation 比较；0=未提供';

COMMIT;
