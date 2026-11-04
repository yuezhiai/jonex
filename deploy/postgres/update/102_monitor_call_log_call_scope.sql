-- ============================================================
-- 102_monitor_call_log_call_scope.sql
-- 平台监控：调用记录补「调用边界」列（存量库增量 DDL，幂等可重跑）
--
-- 编号说明：原拟 084，但 origin/dev 已占用 084_ontology_skill_action_tools.sql；
-- 085–101 亦被其他分支占用，故取跨分支第一个空闲编号 102，避免合并时撞号。
--
-- 为什么需要
--   调用监控有「外部依赖 / 开放能力」两个页签，但物理表只有 direction
--   （outbound / inbound），切不开「外部依赖」与「平台内部服务互调」：
--   任务中心派活给运行方、运行方回报控制面也记 outbound，于是外部依赖页
--   会混进「知识库 / 任务中心」。业务口径要求外部依赖页只显示平台调用的
--   外部系统，内部互调保留在「调用记录」供运维排查。
--
-- 取值（与 direction 正交，见 jonex_core/common/monitor_models.py::CallScope）
--   external 平台调外部系统（模型 / 外部服务）
--   internal 平台内部服务之间
--   open     外部调用方进入平台能力（direction=inbound）
--
-- 历史回填规则（一次性；只处理 call_scope IS NULL 的行）
--   direction='inbound'            -> open
--   direction='outbound' + protocol='openai' -> external（llm-gateway 模型调用）
--   direction='outbound' 其余      -> internal（含 protocol 为空的任务派活/回报）
--
-- 滚动发布安全
--   列**允许 NULL**，不设 NOT NULL / 默认值：旧采集器不认识本列也能继续写，
--   不会因缺列/缺值让整批 INSERT 失败。读侧「外部依赖」只认 external，
--   NULL 行暂时不展示（对模型调用是可见性缺口，不是数据丢失）。等所有
--   采集端升级、观察一整个业务周期无新增 NULL 后，可另开迁移收 NOT NULL。
--
-- 发布顺序
--   1) 本迁移先执行（加列 + 回填）；
--   2) 升级共享采集器（monitor_collector，含漏标兜底）；
--   3) 升级各写入方（llm-gateway / sidecar / task_runtime / runner_adapter）；
--   4) 最后发读侧 / 前端。
--
-- ⚠️ 发布后的补回填（必须人工执行一次）
--   本迁移在「采集端升级前」执行，只能回填当时已存在的行；升级窗口内旧采集器
--   新写入的行仍是 NULL。等所有采集端升级完（观察一整个业务周期无新增 NULL）后，
--   手动执行下面这段把窗口期 NULL 补齐（apply.sh 有版本表，不会重跑本文件）：
--
--     UPDATE monitor.call_log
--     SET call_scope = CASE
--         WHEN direction = 'inbound' THEN 'open'
--         WHEN protocol = 'openai' THEN 'external'
--         ELSE 'internal'
--     END
--     WHERE call_scope IS NULL;
--
-- 回滚
--   ALTER TABLE monitor.call_log DROP COLUMN IF EXISTS call_scope;
--   （读侧只按该列过滤，回滚列前先把读侧回退到旧版本）
-- ============================================================

BEGIN;

ALTER TABLE monitor.call_log
    ADD COLUMN IF NOT EXISTS call_scope VARCHAR(16) NULL;

-- 回填历史行。幂等：只碰 NULL，回填后不再命中。
UPDATE monitor.call_log
SET call_scope = CASE
    WHEN direction = 'inbound' THEN 'open'
    WHEN protocol = 'openai' THEN 'external'
    ELSE 'internal'
END
WHERE call_scope IS NULL;

-- CHECK 约束不能 IF NOT EXISTS，先 DROP 再 ADD 保证可重复执行。
-- 允许 NULL（滚动发布期旧采集器写入的行）；非 NULL 时必须与 direction 自洽。
ALTER TABLE monitor.call_log
    DROP CONSTRAINT IF EXISTS ck_monitor_call_log_scope;
ALTER TABLE monitor.call_log
    ADD CONSTRAINT ck_monitor_call_log_scope
    CHECK (
        call_scope IS NULL
        OR (direction = 'inbound' AND call_scope = 'open')
        OR (direction = 'outbound' AND call_scope IN ('external', 'internal'))
    );

-- 外部依赖页按 call_scope + 时间筛，补一条前导列索引。
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_scope_started
    ON monitor.call_log (call_scope, logical_started_at DESC);

COMMENT ON COLUMN monitor.call_log.call_scope IS
    '调用边界：external(外部依赖)/internal(内部互调)/open(开放能力)；与 direction 正交';

COMMIT;
