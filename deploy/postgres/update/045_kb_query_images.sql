-- ============================================================
-- 045_kb_query_images.sql
-- 以图问知识：查询图片表 + 历史-图片关联表（存量库）
--
-- 对应全量 DDL: deploy/postgres/migrations/004_knowledge_base.sql
-- 对应注释:     deploy/postgres/migrations/007_comments.sql
-- 设计文档:     docs/knowledge-base/image-query-search-execution-plan.md §4-B11
--
-- 做什么
--   新增两张表，**不动** knowledge_search_history（历史表一列不加）：
--     1) knowledge_query_images            查询图片本体（对象键 / MIME / sha256 / 识别结果）
--     2) knowledge_search_history_images   历史 ⇄ 图片 多对多关联
--
-- 为什么是两张表
--   一条历史可以附多张图（上限 IMAGE_QUERY_MAX_IMAGES，默认 4），
--   一张图也可以被多条历史引用（历史「重新检索」、同图再搜）。
--   一对一塞进历史表就表达不了后者，且历史表已经被大量读写，加列影响面更大。
--
-- 为什么关联表不做软删
--   引用是否有效**一律以 knowledge_search_history.is_deleted 为准**（关联行离开历史没有语义）。
--   图片的存活判据是「至少还有一条未删除的历史引用它」，所以清理时判的是 join 后的历史行。
--
-- 为什么有 purged_at / object_deleted 两列（而不是直接删行）
--   删图片 = 软删数据库行 + 删对象存储对象，两件事在两个系统里，无法原子完成。
--   顺序写死为「先条件软删行（拿到该删哪些对象），再删对象」：
--     - object_deleted=0 且 is_deleted=1 → 对象还没删掉，由兜底扫描补删（见 B12）；
--     - 删不存在的对象视为成功（B9 第 2 步 put_bytes 失败时对象可能根本没写成功）。
--   没有这两列就只能靠「行在不在」判断，中断一次就永远漏掉那个对象。
--
-- 幂等
--   两张表都是 CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS，
--   重复执行是空操作。只新增表、不改历史表，回滚无需逆向 DDL。
--
-- 与任务中心的编号让位
--   042 由 042_knowledge_task_center.sql、043 由 043_task_center_permission.sql、
--   044 由 044_task_center_jonex_wiki.sql 占用（均已提交），故本脚本取 045。
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. 查询图片本体
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_query_images (
    id               VARCHAR(64)  PRIMARY KEY,
    tenant_id        VARCHAR(64)  NOT NULL,
    user_id          VARCHAR(128) NOT NULL,
    storage_backend  VARCHAR(16)  NOT NULL,          -- local / cos / s3（落盘时的全局后端）
    storage_key      VARCHAR(512) NOT NULL,
    mime_type        VARCHAR(64)  NOT NULL,          -- magic bytes 判定的真实类型
    file_size        INTEGER      NOT NULL,
    sha256           VARCHAR(64)  NOT NULL,
    recognized_query TEXT,                           -- 合并后的检索查询文本
    description      TEXT,                           -- VLM 客观描述
    ocr_text         TEXT,                           -- VLM 抄录的图中文字
    entities         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    prompt_version   VARCHAR(16),                    -- 提示词版本；改提示词即失效旧结果
    vlm_model        VARCHAR(128),
    purged_at        TIMESTAMP,                      -- 被判定无引用、软删的时刻
    object_deleted   SMALLINT     NOT NULL DEFAULT 0, -- 对象存储里的文件已删除为 1；
                                                      -- 软删后删对象失败时保持 0，由兜底扫描补删
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 最后一次被引用
    is_deleted       SMALLINT     NOT NULL DEFAULT 0
);

-- 同用户同图只存一份（并发落盘靠它兜底：插入撞唯一索引 → 改用已有行）
-- ⚠️ 带 is_deleted=0 的部分索引：软删过的旧图不挡新图落盘
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_qimg_tenant_user_sha
    ON knowledge_base.knowledge_query_images (tenant_id, user_id, sha256)
    WHERE is_deleted = 0;

-- 兜底扫描「无引用的老图」：按 updated_at 过宽限期筛
CREATE INDEX IF NOT EXISTS idx_kb_qimg_sweep
    ON knowledge_base.knowledge_query_images (updated_at)
    WHERE is_deleted = 0;

-- 兜底扫描「已软删、对象尚未删掉的行」
CREATE INDEX IF NOT EXISTS idx_kb_qimg_pending_object_delete
    ON knowledge_base.knowledge_query_images (purged_at)
    WHERE is_deleted = 1 AND object_deleted = 0;

-- ------------------------------------------------------------
-- 2. 检索历史 ⇄ 查询图片 多对多
--    关联行不做软删：引用是否有效以 knowledge_search_history.is_deleted 为准
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_base.knowledge_search_history_images (
    history_id  VARCHAR(64)  NOT NULL,               -- 与 knowledge_search_history.id 同类型
    image_id    VARCHAR(64)  NOT NULL,
    tenant_id   VARCHAR(64)  NOT NULL,
    user_id     VARCHAR(128) NOT NULL,
    sort_order  SMALLINT     NOT NULL DEFAULT 0,     -- 用户附图顺序
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (history_id, image_id)
);

-- 反向查「这张图被哪些历史引用」（软删判定与兜底扫描都走它）
CREATE INDEX IF NOT EXISTS idx_kb_hist_img_image
    ON knowledge_base.knowledge_search_history_images (image_id);

-- 按历史批量取图（历史列表页）
CREATE INDEX IF NOT EXISTS idx_kb_hist_img_history
    ON knowledge_base.knowledge_search_history_images (history_id);

COMMENT ON TABLE knowledge_base.knowledge_query_images IS '以图问知识的查询图片：按 tenant+user 隔离；仅保存历史时落盘；无未删除历史引用时删除对象并软删';
COMMENT ON TABLE knowledge_base.knowledge_search_history_images IS '检索历史与查询图片的关联（多对多），引用是否有效以历史行 is_deleted 为准';

COMMIT;
