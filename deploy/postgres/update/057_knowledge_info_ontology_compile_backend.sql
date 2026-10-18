-- ============================================================
-- 057_knowledge_info_ontology_compile_backend.sql
-- 库级「compile 步走哪个后端」（存量库）
--
-- 背景（P2「本体编译独立容器」）
--   compile 步（本体抽取）原先只有一条去处：atomic-rag 的 `retry_ontology_extract`。
--   P2 把它剥成独立容器（能力 ID `atomic.ontology_compile.v1`，容器
--   ontology-compile:8009），灰度要能**按库**选走哪一条 —— 照 045 的成熟模式
--   （全局单值改一头会静默打掉另一头：2026-10-09 ESWiki 事故）。
--
-- 语义（代码侧：capabilities/knowledge_base/services/compile_backend.py）
--   取值 `rag` / `ontology_compile`；**NULL = 跟随服务端开关**
--   `ONTOLOGY_COMPILE_BACKEND`（缺省 `rag`，存量行全 NULL ⇒ 行为与从前逐字一致）。
--   非 NULL 即**覆盖** env。提交时读、写进文档行（extra_metadata.ontology_compile_backend，
--   与 rag_task_id 同一次 commit，粘性）；改库设置只影响**下一次提交**，在途文档不受影响、
--   无需重启任何服务。查询侧（对账 EXTRACTING 轮询）只读那个粘性值。
--
-- ⚠ 与 045 的列是**两把钥匙、两个语义**：`ingest_backend` 管**解析链**，
--   本列管 **compile 步**；两列互不覆盖、互不回落。
--
-- 为什么只写 update/、不并回全量脚本
--   `004_knowledge_base.sql` 已线上发布，照先例只碰注释、不并回。
--   ⚠ 全新的库仍要跑一次 `make db-migrate`（deploy/README.md 的既有条款）。
--
-- 发版次序：**先 `make db-migrate`，再换 knowledge-base 新镜像** ——
--   反序时新代码读 `KnowledgeInfo` 实体会撞上不存在的列（库级派单值那条
--   `select(KnowledgeInfo.ontology_compile_backend)` 当场 UndefinedColumn 硬失败）。
--
-- 回滚：`UPDATE knowledge_base.knowledge_info SET ontology_compile_backend = NULL;`
--   （读时求值，即刻回到「跟随开关」）；或（本期无 API）直接改回 env
--   `ONTOLOGY_COMPILE_BACKEND=rag` 并重启 knowledge-base-service。
--
-- 幂等: ADD COLUMN IF NOT EXISTS / COMMENT 重复执行均为空操作。
-- ============================================================

BEGIN;

-- 057：库级「compile 步走哪个后端」（rag = atomic-rag 现状 / ontology_compile = 新服务）
-- 与 045 同形态：NULL = 跟随服务端开关 ONTOLOGY_COMPILE_BACKEND（缺省 rag）。
-- ⚠ 这是**提交时的派单输入**，不是查询侧的粘性落点（粘性值在文档行 extra_metadata）。
ALTER TABLE knowledge_base.knowledge_info
    ADD COLUMN IF NOT EXISTS ontology_compile_backend VARCHAR(32);
COMMENT ON COLUMN knowledge_base.knowledge_info.ontology_compile_backend IS
    'compile 步后端派单值：rag（atomic-rag）/ ontology_compile（新服务）/ NULL=跟随 env';

COMMIT;
