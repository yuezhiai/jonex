/** W2.5 质量闭环纯函数：标签、颜色、权限与结果 JSON 解析。 */
import type {
  DgQualityCheckType,
  DgQualityDimension,
  DgQualityIssue,
  DgQualityIssueStatus,
  DgQualityResultStatus,
  DgQualityResultSubmitItem,
  DgQualityRule,
  DgQualitySeverity,
} from './types';

export const QUALITY_DIMENSIONS: DgQualityDimension[] = [
  'completeness', 'uniqueness', 'validity', 'consistency', 'timeliness',
];
export const QUALITY_SEVERITIES: DgQualitySeverity[] = [
  'info', 'warning', 'error', 'critical',
];
export const QUALITY_CHECK_TYPES: DgQualityCheckType[] = ['library', 'sql', 'custom'];
export const QUALITY_ISSUE_STATUSES: DgQualityIssueStatus[] = [
  'open', 'assigned', 'fixing', 'pending_verify', 'closed',
];
export const QUALITY_RESULT_STATUSES: DgQualityResultStatus[] = [
  'passed', 'failed', 'error',
];

/** 内置规则指标词表；标签走 `dataGovernance.quality.metric.*`。 */
export const QUALITY_METRICS = [
  'rowCount', 'nullValues', 'missingValues', 'duplicateValues', 'invalidValues', 'unique',
] as const;

/** 工单事件类型词表；标签走 `dataGovernance.quality.issueEvent.*`。 */
export const QUALITY_ISSUE_EVENTS = [
  'created', 'assigned', 'status_change', 'verified', 'reopened', 'comment',
] as const;

/** `t` 的最小形状，便于在纯函数里做本地化而不依赖 i18next 类型。 */
export type QualityTranslate = (
  key: string,
  params?: Record<string, string | number>,
) => string;

export function metricLabelKey(metric: string): string {
  return `dataGovernance.quality.metric.${metric}`;
}

export function issueEventLabelKey(eventType: string): string {
  return `dataGovernance.quality.issueEvent.${eventType}`;
}

export function dimensionLabelKey(dimension: string): string {
  return `dataGovernance.quality.dimension.${dimension}`;
}

export function severityLabelKey(severity: string): string {
  return `dataGovernance.quality.severity.${severity}`;
}

export function statusLabelKey(status: string): string {
  return `dataGovernance.quality.issueStatus.${status}`;
}

export function checkTypeLabelKey(checkType: string): string {
  return `dataGovernance.quality.checkType.${checkType}`;
}

export function severityColor(severity: string): string {
  switch (severity) {
    case 'critical': return 'red';
    case 'error': return 'volcano';
    case 'warning': return 'orange';
    default: return 'blue';
  }
}

export function statusColor(status: string): string {
  switch (status) {
    case 'open': return 'red';
    case 'assigned': return 'orange';
    case 'fixing': return 'blue';
    case 'pending_verify': return 'purple';
    case 'closed': return 'green';
    default: return 'default';
  }
}

export function runStatusColor(status: string): string {
  if (status === 'completed') return 'green';
  if (status === 'failed') return 'red';
  return 'processing';
}

/** 管理员可操作全部；普通用户只可操作指派给自己的工单。 */
export function isIssueActionAllowed(
  issue: Pick<DgQualityIssue, 'assigneeId'>,
  userId: string | null | undefined,
  isAdmin: boolean,
): boolean {
  if (isAdmin) return true;
  return Boolean(userId && issue.assigneeId && issue.assigneeId === userId);
}

/** 手工状态流转允许的目标（与后端状态机一致）。 */
export function issueStatusTransitions(status: DgQualityIssueStatus): DgQualityIssueStatus[] {
  switch (status) {
    case 'open': return ['assigned', 'fixing'];
    case 'assigned': return ['fixing'];
    case 'fixing': return ['pending_verify'];
    case 'pending_verify': return ['fixing'];
    default: return [];
  }
}

/** 规则摘要：library 显示 metric（+参数），其余显示本地化类型名。 */
export function ruleSummary(
  rule: Pick<DgQualityRule, 'checkType' | 'definition'>,
  translate: QualityTranslate,
): string {
  if (rule.checkType !== 'library') return translate(checkTypeLabelKey(rule.checkType));
  const metric = typeof rule.definition.metric === 'string' ? rule.definition.metric : '';
  const metricKey = metricLabelKey(metric);
  const translated = metric ? translate(metricKey) : '';
  // 未知指标（后端新增但前端未跟词表）退回原始编码，绝不显示裸 key。
  const label = translated && translated !== metricKey ? translated : metric;
  const missing = rule.definition.missingValues;
  if (Array.isArray(missing) && missing.length > 0) {
    const separator = translate('dataGovernance.quality.rule.valueSeparator');
    const values = missing.slice(0, 3).join(separator);
    return translate('dataGovernance.quality.rule.missingValues', {
      label: label || translate(checkTypeLabelKey('library')),
      values: `${values}${missing.length > 3 ? '…' : ''}`,
    });
  }
  return label || translate(checkTypeLabelKey('library'));
}

// ── 资产 ID 解析 / 拼接（与后端 `split_asset_id` 同口径） ───────────────

export interface QualityAssetParts {
  dataSourceId: string;
  table: string;
  column: string | null;
}

/** 解析 `{ds}:{table}` / `{ds}:{table}.{column}`；列名按最后一个点切分。 */
export function parseQualityAssetId(
  assetId: string,
  assetType: 'table' | 'column',
): QualityAssetParts {
  const value = assetId.trim();
  const colon = value.indexOf(':');
  if (colon <= 0 || colon === value.length - 1) {
    return { dataSourceId: '', table: '', column: null };
  }
  const dataSourceId = value.slice(0, colon);
  const rest = value.slice(colon + 1);
  if (assetType !== 'column') {
    return { dataSourceId, table: rest, column: null };
  }
  const dot = rest.lastIndexOf('.');
  if (dot <= 0 || dot === rest.length - 1) {
    return { dataSourceId, table: '', column: null };
  }
  return {
    dataSourceId,
    table: rest.slice(0, dot),
    column: rest.slice(dot + 1),
  };
}

/** 拼接资产 ID；`column` 为空 = 表级资产。 */
export function buildQualityAssetId(
  dataSourceId: string,
  table: string,
  column?: string | null,
): string {
  const base = `${dataSourceId}:${table}`;
  return column ? `${base}.${column}` : base;
}

// ── 巡检 SQL 文本（预览 / 快照共用） ───────────────────────────────────

export interface QualitySqlLike {
  name: string;
  sql: string | null;
}

/**
 * 把 SQL 列表拼成可复制/下载的文本；`sql=null` 的规则用调用方给的
 * 本地化占位文案（外部执行器规则没有 SQL）。
 */
export function buildSqlText(items: QualitySqlLike[], externalLabel: string): string {
  return items
    .map((item) => `-- ${item.name || item.sql || ''}\n${item.sql ?? externalLabel}`)
    .join('\n\n');
}

/** 下载文件名：有 runId 时带上它，否则用通用名并清理非法字符。 */
export function sqlFilename(runId?: string | null): string {
  const safe = (runId ?? '').trim().replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
  return safe ? `quality-run-${safe}.sql` : 'quality-rules.sql';
}

export function formatMetricValue(value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '-';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  try {
    return JSON.stringify(value);
  } catch {
    return '-';
  }
}

export interface SplitResultsOk {
  ok: true;
  items: DgQualityResultSubmitItem[];
}

export interface SplitResultsError {
  ok: false;
  error: string;
}

/**
 * 解析外部执行器粘贴的结果 JSON。
 *
 * 接受裸数组或 `{results: [...]}`；每条必须有 `rule_id`/`ruleId` 与合法
 * `status`。无法安全解析时回 `{ok:false,error}`，由页面提示而不是抛异常。
 */
export function splitResultsJson(text: string): SplitResultsOk | SplitResultsError {
  if (!text.trim()) return { ok: false, error: 'empty' };
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { ok: false, error: 'invalid_json' };
  }
  const list = Array.isArray(raw)
    ? raw
    : (raw && typeof raw === 'object' && Array.isArray((raw as { results?: unknown }).results)
      ? (raw as { results: unknown[] }).results
      : null);
  if (!list) return { ok: false, error: 'not_array' };
  const items: DgQualityResultSubmitItem[] = [];
  for (const entry of list) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      return { ok: false, error: 'invalid_item' };
    }
    const row = entry as Record<string, unknown>;
    const ruleId = typeof row.rule_id === 'string'
      ? row.rule_id
      : (typeof row.ruleId === 'string' ? row.ruleId : '');
    const status = row.status;
    if (!ruleId || !QUALITY_RESULT_STATUSES.includes(status as DgQualityResultStatus)) {
      return { ok: false, error: 'invalid_item' };
    }
    items.push({
      ruleId,
      status: status as DgQualityResultStatus,
      ...(row.metric_value !== undefined ? { metricValue: row.metric_value } : {}),
      ...(typeof row.message === 'string' ? { message: row.message } : {}),
      ...(Array.isArray(row.sample) ? { sample: row.sample } : {}),
    });
  }
  return { ok: true, items };
}
