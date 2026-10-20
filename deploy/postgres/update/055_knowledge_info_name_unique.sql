-- =============================================================================
-- [jonex] 知识库名称同空间唯一：partial unique index
--
-- 语义：同一领域空间内知识库不可重名。软删记录不占名（删除后可用同名再建），
--       与 `knowledge_base.services`（`idx_kb_svc_unique_name`）、
--       `knowledge_base.folders` / `tags` 的既有行为一致。
--
-- 与知识库能力里的 `KnowledgeInfoService._ensure_name_unique`
-- （`capabilities/knowledge_base/services/knowledge_info_service.py`）**同口径**：
--   tenant_id + space_id + name + is_deleted = 0
-- 改一边必须改另一边，否则两层判定分叉。
--
-- -----------------------------------------------------------------------------
-- ⚠️ 与「领域服务名称唯一」那次修复（只加索引、无数据动作）的关键差别：
--    **本脚本不自动去重。**
--
--    `services` 是轻量容器，重复行删掉就完事；`knowledge_info` 是重容器 ——
--    文档（knowledge_documents）/ 文件夹 / 标签 / 本体 / 向量 / 检索历史都挂在
--    它下面。删一行不是删一行，是一次连带的数据销毁，且"该保留哪一条"只有人
--    能判断（哪条有真数据、哪条是误建）。
--
--    所以本脚本的行为是：**有重名就报错中止**。
--    apply.sh 以 `-v ON_ERROR_STOP=1` 执行、且**只有成功的文件才登记进
--    `public.schema_migrations`** —— 报错的文件不会被记为已应用，人工处理完
--    重跑 `make db-migrate` 会重试它。
--
--    ⚠️ 刻意**不**用「检测到重名就跳过建索引」的软着陆写法：那正是本次问题的
--       成因（三层都没拦，于是规则等于不存在）。宁可让迁移卡住可见，也不要
--       留下一个"看起来修过了"的半修复状态。
--
--    存量重名的处理步骤见 `docs/knowledge-base/` 与本次分析文档
--    `E:\ZYwork\任务表\知识库重名未拦截-第一性原理分析与最小必要方案.md` 第 5 节。
-- =============================================================================

-- ① 冲突先列出来（进迁移日志，供人工定位；无冲突时不输出任何行）
SELECT tenant_id,
       space_id,
       name,
       count(*)                            AS cnt,
       string_agg(id, ', ' ORDER BY created_at) AS kb_ids
  FROM knowledge_base.knowledge_info
 WHERE is_deleted = 0
 GROUP BY tenant_id, space_id, name
HAVING count(*) > 1
 ORDER BY cnt DESC, name;

-- ② 仍有冲突则中止（不建索引、不登记迁移）
DO $$
DECLARE
    dup_groups integer;
BEGIN
    SELECT count(*) INTO dup_groups FROM (
        SELECT 1
          FROM knowledge_base.knowledge_info
         WHERE is_deleted = 0
         GROUP BY tenant_id, space_id, name
        HAVING count(*) > 1
    ) t;

    IF dup_groups > 0 THEN
        RAISE EXCEPTION
            'knowledge_info 仍有 % 组同空间同名知识库，已中止本迁移。请先人工处理（建议改名而非删除），处理方式见 055 脚本头部注释与任务表的分析文档，然后重跑 make db-migrate。',
            dup_groups;
    END IF;
END $$;

-- ③ partial unique index
--    仅约束未删除记录（`WHERE is_deleted = 0`），与 folder / tag / service 一致：
--    软删之后同名可以再建，不会让历史名字永久占位。
--    命名沿用同族 `uq_kb_folders_name_per_kb` / `uq_kb_tags_name_per_kb`。
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_info_name_per_space
    ON knowledge_base.knowledge_info (tenant_id, space_id, name)
    WHERE is_deleted = 0;

-- 同步说明：全新库的 DDL 已在 `deploy/postgres/migrations/004_knowledge_base.sql`
-- 的 `knowledge_info` 索引区补了同一条语句（项目约定：`update/` 增量与
-- `migrations/` 全量终态必须一致）。
-- 存量库跑完本脚本后，两边的索引定义逐字相同。
