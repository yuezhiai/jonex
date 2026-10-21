-- ============================================================
-- 050_platform_monitor.sql
-- 平台监控 · 外部调用明细（存量库）：新建 monitor schema + N1 逻辑调用 + N2 物理 Attempt
--
-- 对应全量：
--   deploy/postgres/migrations/001_schemas.sql  → CREATE SCHEMA monitor
--   deploy/postgres/migrations/002_platform.sql → 建 monitor.call_log / monitor.call_attempt + 索引
-- 新库由上面两处直接建立；本脚本只服务**已经跑过它们**的存量库。
--
-- 为什么存量库要单独补
--   migrations/ 只在数据卷**首次初始化**时执行（/docker-entrypoint-initdb.d），
--   存量库不会回头建表 —— 不补这里，调用监控查询会一直报「表不存在」。
--
-- 边界（有意不做的事）
--   * 不含任何 ALTER TABLE metering.*（N3「Token 幂等」本期不做，llm_usage_log 保持原样）
--   * 不含分区 / 清理语句（保留期数值待产品负责人冻结，本期已知缺口）
--   * 两表均**不建 space_id 列**（不按空间维度采集）
--   * call_log.protocol / call_attempt.status 取值集未定，本轮不写 CHECK
--   * request_id **不加外键**（采集异步，N2 可能先于 N1 落库）
--   * 全部语句 IF NOT EXISTS，可重复执行（apply.sh 重复执行安全）
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS monitor;

-- ============================================================
-- N1：逻辑调用表 monitor.call_log
-- 一行 = 一次逻辑调用；request_id 在**本表内**唯一（降级模式下改由 call_attempt 取数，两表语义不同）
-- ============================================================
CREATE TABLE IF NOT EXISTS monitor.call_log (
    id                  BIGSERIAL     PRIMARY KEY,
    request_id          VARCHAR(64)   NOT NULL,
    trace_id            VARCHAR(64)   NULL,
    tenant_id           VARCHAR(64)   NOT NULL,
    direction           VARCHAR(16)   NOT NULL,
    source              VARCHAR(128)  NULL,
    target              VARCHAR(128)  NULL,
    protocol            VARCHAR(32)   NULL,
    trigger_type        VARCHAR(16)   NOT NULL DEFAULT 'user',
    logical_started_at  TIMESTAMPTZ   NULL,
    final_ended_at      TIMESTAMPTZ   NULL,
    final_status        VARCHAR(16)   NOT NULL,
    error_code          VARCHAR(64)   NULL,
    error_message       TEXT          NULL,
    attempt_count       INTEGER       NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_monitor_call_log_request_id UNIQUE (request_id),
    -- 枚举取值集未定（direction / final_status）；定稿后同步本 CHECK
    CONSTRAINT ck_monitor_call_log_direction  CHECK (direction IN ('outbound','inbound')),
    CONSTRAINT ck_monitor_call_log_final_status
        CHECK (final_status IN ('success','failed','cancelled','running','unknown')),
    -- 触发方取值集已定（user 用户直接发起 / system 系统后台批量）
    CONSTRAINT ck_monitor_call_log_trigger CHECK (trigger_type IN ('user','system'))
);

-- I2：时间范围 + 倒序分页（调用列表默认按调用时间倒序）
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_tenant_started
    ON monitor.call_log (tenant_id, logical_started_at DESC);
-- I3：凭 Trace ID 关联完整链路
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_trace
    ON monitor.call_log (trace_id);
-- I4：按方向 / 状态筛选 + 时间倒序
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_dir_status_started
    ON monitor.call_log (direction, final_status, logical_started_at DESC);
-- I5：按对象 / 能力筛选与 TOP 统计
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_target_started
    ON monitor.call_log (target, logical_started_at DESC);
-- I8：as-of / 最近写入查询（数据新鲜度取 MAX(created_at)）
CREATE INDEX IF NOT EXISTS idx_monitor_call_log_created
    ON monitor.call_log (created_at DESC);

-- ============================================================
-- N2：物理 Attempt 表 monitor.call_attempt
-- request_id **不唯一**（同一逻辑调用多次尝试）—— 降级模式的取数表
-- ============================================================
CREATE TABLE IF NOT EXISTS monitor.call_attempt (
    id                  BIGSERIAL     PRIMARY KEY,
    attempt_id          VARCHAR(64)   NOT NULL,
    request_id          VARCHAR(64)   NOT NULL,
    attempt_no          INTEGER       NOT NULL,
    tenant_id           VARCHAR(64)   NOT NULL,
    attempt_started_at  TIMESTAMPTZ   NULL,
    attempt_ended_at    TIMESTAMPTZ   NULL,
    status              VARCHAR(16)   NOT NULL,
    error_code          VARCHAR(64)   NULL,
    error_message       TEXT          NULL,
    is_final            BOOLEAN       NOT NULL DEFAULT FALSE,
    wait_before_ms      INTEGER       NULL,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_monitor_call_attempt_id UNIQUE (attempt_id),
    CONSTRAINT uq_monitor_call_attempt_req_no UNIQUE (request_id, attempt_no)
);

-- I6：由 N1 关联 N2 Attempt 明细
CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_request
    ON monitor.call_attempt (request_id);
-- I7：降级模式按 Attempt 分桶 / 查询
CREATE INDEX IF NOT EXISTS idx_monitor_call_attempt_started
    ON monitor.call_attempt (attempt_started_at DESC);

COMMIT;
