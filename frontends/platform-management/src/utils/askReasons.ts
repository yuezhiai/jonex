/** ② 数据源问答失败原因码 → 中文文案。
 *
 * mirror onto-intelligent/src/fanout.py `_ASK_REASONS`（同一张表，勿单边改）；
 * 未知码原样显示（不隐藏信息）。操作日志详情与演化台共用。
 */
export const ASK_REASON_ZH: Record<string, string> = {
  no_candidate: '没命中任何已确认的数据实体（含对齐还没确认）',
  ambiguous: '命中多个实体，需要点名',
  entity_gone: '实体在源里已不存在',
  no_readonly_creds: '该源没登记只读凭据（要去登记）',
  not_a_database_source: '源不是 database 类型 / 引擎不支持',
  source_gone: '数据源已被删',
  not_answerable: '模型认为这些列答不了这个问题',
  rejected_unsafe_sql: '生成的 SQL 未过安全闸（未执行）',
  llm_failed: '生成 SQL 的模型调用失败（可重试）',
  exec_failed: 'SQL 执行失败（可重试）',
};

/** reason_code → 展示文案；空值回空串、未知码回原码。 */
export function askReasonLabel(code: string | null | undefined): string {
  if (!code) return '';
  return ASK_REASON_ZH[code] ?? code;
}
