-- ============================================================
-- 056_drop_jonex_wiki_kb_settings.sql
-- 撤回 P3b 方案 A 在引擎库留下的 KB 级模型配置表（方案 B 不再需要它）
--
-- 设计：docs/knowledge-base/kb-model-settings-design.md §5.3（方案 B）
-- 背景：方案 A 曾用引擎侧 `jonex_wiki.kb_settings` 存 KB 编译模型，并随引擎镜像
--       带了一条 alembic 迁移 `0007_create_kb_settings`。方案 B 改成「模型随任务
--       透传、只存在任务快照里」，迁移文件与 ORM/repo 已从代码里物理删除 ——
--       于是**已经跑过 0007 的库**会留下两个问题：
--         1) 残留 `jonex_wiki.kb_settings` 表（代码不再读写，纯垃圾）；
--         2) `jonex_wiki.alembic_version` 仍写着 `0007`，而新镜像里没有这个
--            revision ⇒ 容器启动时 `alembic upgrade head` 直接报
--            「Can't locate revision identified by '0007'」并退出。
--       这两件事**必须在换引擎镜像之前**处理：迁移文件没了，`alembic downgrade`
--       也没法用，只能手工 SQL 收尾。
--
-- 幂等：DROP TABLE IF EXISTS + 带条件的 UPDATE。重复执行是空操作；
--       从未跑过 0007 的库（例如新装环境）执行它同样安全。
--
-- ⚠ 顺序：本脚本要在 `jonex-wiki` 换新镜像**之前**执行（`make db-migrate`），
--        否则旧镜像重启时会把表又建回来。若表已被旧镜像重建过，重跑本脚本即可。
--
-- 数据影响：kb_settings 只存「KB → 编译模型名」这一份投影，事实源是平台表
--           `knowledge_base.knowledge_model_settings`（055），丢弃它不丢配置。
-- ============================================================

BEGIN;

-- 1) 删掉引擎侧那张表（含主键/索引，随表一起走）
DROP TABLE IF EXISTS jonex_wiki.kb_settings;

-- 2) 把引擎的 alembic 版本号退回 0006（新镜像的 head）
--    只在确实停在 0007 时改，避免误改其它版本状态。
--    新库 / 引擎容器从未启动过的库里没有 `jonex_wiki.alembic_version`（该 schema 由
--    wiki 容器首次启动时建），此时没有 0007 残留可清 —— 直接跳过；
--    否则裸 UPDATE 会 `relation "alembic_version" does not exist` 让整份脚本失败。
DO $$
BEGIN
    IF to_regclass('jonex_wiki.alembic_version') IS NOT NULL THEN
        UPDATE jonex_wiki.alembic_version
           SET version_num = '0006'
         WHERE version_num = '0007';
    ELSE
        RAISE NOTICE 'jonex_wiki.alembic_version 不存在，跳过 alembic 版本号回收';
    END IF;
END
$$;

COMMIT;
