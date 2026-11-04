-- ============================================================
-- 083_monitor_call_log_source_to_user_identity.sql
-- 监控调用记录 source 归一化到稳定标识 user:<本地用户主键>（存量库）
--
-- 背景
--   monitor.call_log.source 记录调用方，历史形态混杂：
--     · 用户名（如 admin）       —— 旧版 Sidecar 入站采集
--     · 裸数字用户主键（如 1）   —— 旧版 llm-gateway 出站采集
--     · 系统标识 apikey / platform / anonymous / unknown / test / jwt / mcp / system
--     · 空串
--   用户名可变、且只在租户内唯一，写进记录后用户一改名，同一个人就分裂成两条
--   来源；裸数字与用户名并存时同一个账号还会在筛选下拉里出现两次。
--
-- 终态口径
--   采集端今后只写稳定标识 `user:<本地用户主键>`（BIGSERIAL 全局唯一，改名不变），
--   展示名由读侧按主键解析。本脚本把存量里「能对应到真实用户」的行一次性归一：
--     · 用户名形态  → user:<id>
--     · 裸数字形态  → user:<id>
--     · 已是 user:<id> 的行原样跳过（幂等）
--     · 系统标识、空串、解析不到真实用户的行一律保持原值
--
-- ============================================================
-- ⚠️ 与 082 的关系（必读）
--   本脚本**完全吸收** 082_monitor_call_log_source_normalization.sql 的目标，
--   并把方向改成稳定标识。082 只是过渡脚本（裸数字 → 用户名）。
--   **应用 083 之后不应再单独执行 082**：082 会把当时仍存在的裸数字改写成用户名，
--   把刚归一好的数据重新打散成「用户名 + user:<id>」混合形态；而 083 已经执行完，
--   不会再回头覆盖这些行。
--   批量执行（apply.sh 按文件名排序）时 082 会先跑、083 随后跑，可行且终态正确，
--   但更推荐直接跳过 082。
--   排查本机是否已登记 082（历史环境可能是手工跑 SQL、未登记版本表）：
--     SELECT filename, applied_at FROM public.schema_migrations
--     WHERE filename LIKE '082%' OR filename LIKE '083%' ORDER BY filename;
--   若 082 未登记，`make db-migrate` 会把它当作待应用文件、排在 083 之前执行一次：
--   同一批次里可行（083 随后覆盖其结果），但**不要**在 083 应用之后单独再跑 082。
--   若两者同批应用，回滚顺序必须「先 083 备份、后 082 备份」（见回滚段）。
-- ============================================================
--
-- 执行前提（⚠️ 顺序不能反：先堵后清）
--   1) 本环境所有写 source 的采集端（Sidecar / llm-gateway / 其它旁路）已升级到
--      只写 `user:<id>` 的版本；
--   2) 观察至少一个业务周期，确认不再新增用户名形态与裸数字形态：
--        -- 用户名形态（应逐日归零）
--        SELECT date_trunc('day', c.created_at) d, count(*)
--        FROM monitor.call_log c
--        JOIN platform.users u
--          ON u.tenant_id = c.tenant_id AND u.username = c.source AND u.is_deleted = 0
--        WHERE c.source !~ '^user:[0-9]{1,18}$'
--        GROUP BY 1 ORDER BY 1;
--        -- 裸数字形态（应逐日归零；上面那条按用户名关联，抓不到裸数字）
--        SELECT date_trunc('day', created_at) d, count(*)
--        FROM monitor.call_log
--        WHERE source ~ '^[0-9]{1,18}$'
--        GROUP BY 1 ORDER BY 1;
--      最后一整天无新行后再执行。先回刷后升级 = 旧采集端会立刻把旧形态写回来。
--
-- 安全边界
--   · 严格按行自身 tenant_id 关联 platform.users，禁止跨租户串号；
--   · 只改 is_deleted = 0 的活跃用户能对应上的行；软删用户的历史行、
--     租户内查无此人的行**保持原值**，不猜测、不跨租户兜底；
--   · 已是 `user:<id>` 的行跳过，重复执行不再命中（幂等）；
--   · 系统标识与空串不参与匹配；
--   · 全表顺序扫描（source 无索引），大表请低峰执行；只对命中行加行锁。
--
-- 回滚（需在应用后手动执行；备份表保留期建议 ≥ 30 天）
--   BEGIN;
--   UPDATE monitor.call_log c SET source = b.source
--   FROM monitor.call_log_source_bak_083 b WHERE c.id = b.id;
--   COMMIT;
--   若 082 与 083 在同一批里都跑过：先按上面从 083 备份恢复，再从 082 备份恢复
--   （082 备份里才是最初的裸数字），否则终态会停在用户名形态。
--
-- 已知边界
--   · 若某用户的 username 恰好形如 `user:123`，该用户名形态的行会被当作已规范
--     形态跳过，不会被归一到它自己的主键（概率极低；如需处理请人工核对该租户）。
--   · 裸数字值与某用户的 username 同形且指向不同用户时（例如存在 username='7'
--     的账号，同时又存在 id=7 的账号），关联可能命中两行，UPDATE ... FROM 会
--     任取其一；执行前建议核对：
--        SELECT tenant_id, id, username FROM platform.users
--        WHERE is_deleted = 0 AND username ~ '^[0-9]{1,18}$';
--   · 系统标识按大小写不敏感比较（读侧解析同口径），因此用户名恰好是 Test /
--     System 这类保留字的账号不会被归一 —— 与系统标识撞名时以系统语义优先。
--   · 备份表固定名：重复执行会补录「首次执行之后新出现」的待归一行，但已备份
--     的行保留首次快照（同一行不会因重跑被覆盖还原点）。
--   · 存量筛选链接/看板若保存了 source=<用户名> 或 source=<数字>，回刷后查不到历史。
-- ============================================================

BEGIN;

-- 备份受影响行（回滚唯一依据）。先建空表取列类型，再由 INSERT 快照：
-- 「建表即备份」的写法在重跑时不会追加首次执行之后新出现的行，回滚就不完整。
CREATE TABLE IF NOT EXISTS monitor.call_log_source_bak_083 AS
SELECT c.id, c.tenant_id, c.source
FROM monitor.call_log c
WHERE false;

INSERT INTO monitor.call_log_source_bak_083 (id, tenant_id, source)
SELECT DISTINCT c.id, c.tenant_id, c.source
FROM monitor.call_log c
JOIN platform.users u
  ON u.tenant_id = c.tenant_id
 AND u.is_deleted = 0
 AND (
       u.username = c.source
       -- 数字 → bigint 的转换放在 CASE 里，避免执行计划把转换提前到正则
       -- 过滤之前而对非数字文本抛 invalid input syntax for type bigint。
       OR u.id = (CASE WHEN c.source ~ '^[0-9]{1,18}$' THEN c.source::bigint END)
     )
WHERE c.source IS NOT NULL
  AND c.source <> ''
  AND c.source !~ '^user:[0-9]{1,18}$'
  AND lower(c.source) NOT IN
      ('apikey', 'platform', 'anonymous', 'unknown', 'test', 'jwt', 'mcp', 'system')
  -- 同一行只留首次快照：重跑时已备份的行不再追加
  AND NOT EXISTS (
      SELECT 1 FROM monitor.call_log_source_bak_083 b WHERE b.id = c.id
  );

-- 归一：用户名形态 / 裸数字形态 → user:<主键>
UPDATE monitor.call_log c
SET source = 'user:' || u.id::text
FROM platform.users u
WHERE u.tenant_id = c.tenant_id
  AND u.is_deleted = 0
  AND (
        u.username = c.source
        OR u.id = (CASE WHEN c.source ~ '^[0-9]{1,18}$' THEN c.source::bigint END)
      )
  AND c.source IS NOT NULL
  AND c.source <> ''
  AND c.source !~ '^user:[0-9]{1,18}$'
  AND lower(c.source) NOT IN
      ('apikey', 'platform', 'anonymous', 'unknown', 'test', 'jwt', 'mcp', 'system');

COMMIT;
