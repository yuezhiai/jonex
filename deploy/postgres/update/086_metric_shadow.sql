-- 指标编译影子对比（W3.3 M4，2026-11-01）：纯追加诊断表，整体幂等。
-- 存量库先 psql -f 本文件，再把 NL2SQL_METRIC_SHADOW 打开（缺表时采样写入只 warning）。
BEGIN;
CREATE TABLE IF NOT EXISTS knowledge_base.nl2sql_metric_shadow (
    id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    knowledge_base_id VARCHAR(64) NOT NULL,
    data_source_id VARCHAR(64) NOT NULL DEFAULT '',
    question VARCHAR(500) NOT NULL,
    entity_name VARCHAR(255),
    main_outcome VARCHAR(16) NOT NULL,
    main_reason_code VARCHAR(64),
    main_row_count INTEGER,
    main_elapsed_ms INTEGER,
    status VARCHAR(32) NOT NULL,
    metric_name VARCHAR(255),
    metric_type VARCHAR(16),
    match_kind VARCHAR(16),
    matched_term VARCHAR(255),
    compiled_sql TEXT,
    shadow_row_count INTEGER,
    shadow_elapsed_ms INTEGER,
    agreement VARCHAR(32) NOT NULL,
    scene JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_kb_n2s_shadow_main CHECK (main_outcome IN ('success', 'failed')),
    CONSTRAINT ck_kb_n2s_shadow_status CHECK (
        status IN ('no_metric', 'not_executable', 'compile_error',
                   'exec_failed', 'exec_ok')
    ),
    CONSTRAINT ck_kb_n2s_shadow_agree CHECK (
        agreement IN ('main_failed_shadow_ok', 'both_failed', 'both_empty',
                      'row_count_mismatch', 'equal_scalar', 'value_mismatch',
                      'unknown', 'not_comparable')
    )
);
CREATE INDEX IF NOT EXISTS idx_kb_n2s_shadow_window
    ON knowledge_base.nl2sql_metric_shadow(tenant_id, knowledge_base_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_n2s_shadow_status
    ON knowledge_base.nl2sql_metric_shadow(tenant_id, status, created_at DESC);
COMMENT ON TABLE knowledge_base.nl2sql_metric_shadow IS
    'W3.3 M4 指标编译影子对比（只记录不替换；不存原始数据值）。';
COMMIT;
