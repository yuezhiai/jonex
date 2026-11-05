/**
 * W2.5 数据质量闭环取数模块。
 *
 * 后端 snake_case 只在本层出现；页面与组件只接触 camelCase。`apiClient`
 * 已解包 `success_response(data=...)`。
 */
import type {
  DgContractDataSourceOption,
  DgQualityIssue,
  DgQualityIssueDetail,
  DgQualityIssueEvent,
  DgQualityIssueListParams,
  DgQualityIssueListResult,
  DgQualityIssueStatus,
  DgQualityPreviewResult,
  DgQualityResultItem,
  DgQualityResultSubmitItem,
  DgQualityRule,
  DgQualityRuleBody,
  DgQualityRuleImportResult,
  DgQualityRuleListParams,
  DgQualityRuleListResult,
  DgQualityRuleSnapshotItem,
  DgQualityRuleUpdateBody,
  DgQualityRun,
  DgQualityRunCreateBody,
  DgQualityRunCreateResult,
  DgQualityRunDetail,
  DgQualityRunListParams,
  DgQualityRunListResult,
  DgQualityRunSubmitResult,
  DgQualitySeverity,
  DgQualitySqlItem,
  DgQualitySummary,
  DgQualitySummaryItem,
} from '@/pages/DataGovernance/types';
import { listContractDataSources } from './dataContracts';
import { listKbOptions } from './dataOperations';
import apiClient from './request';

const BASE = '/data-governance/quality';

export interface QualityRuleRowRaw {
  id: string;
  name: string;
  asset_type: 'table' | 'column';
  asset_id: string;
  data_source_id: string;
  kb_id?: string | null;
  dimension: string;
  severity: string;
  check_type: string;
  definition?: unknown;
  threshold?: unknown;
  enabled?: number | boolean | null;
  contract_id?: string | null;
  revision?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface QualityRunRowRaw {
  id: string;
  data_source_id: string;
  trigger?: string | null;
  status: string;
  rule_ids?: unknown;
  /** 仅详情接口返回；列表为省略字段。 */
  rule_snapshot?: unknown;
  executor?: string | null;
  dialect?: string | null;
  total?: number | null;
  passed?: number | null;
  failed?: number | null;
  error?: number | null;
  message?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
}

export interface QualityResultRowRaw {
  rule_id: string;
  asset_type: 'table' | 'column';
  asset_id: string;
  status: string;
  metric_value?: unknown;
  message?: string | null;
  sample?: unknown;
  checked_at?: string | null;
}

export interface QualityIssueRowRaw {
  id: string;
  rule_id: string;
  asset_type: 'table' | 'column';
  asset_id: string;
  data_source_id: string;
  severity: string;
  status: string;
  assignee_id?: string | null;
  title: string;
  detail?: string | null;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
  resolved_at?: string | null;
  revision?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface QualityIssueEventRowRaw {
  id: string;
  issue_id: string;
  event_type: string;
  from_status?: string | null;
  to_status?: string | null;
  actor_id?: string | null;
  note?: string | null;
  created_at?: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function recordOrEmpty(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function listOrEmpty(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function numberOr(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

export function mapQualityRule(row: QualityRuleRowRaw): DgQualityRule {
  return {
    id: String(row.id),
    name: String(row.name ?? ''),
    assetType: row.asset_type === 'table' ? 'table' : 'column',
    assetId: String(row.asset_id ?? ''),
    dataSourceId: String(row.data_source_id ?? ''),
    kbId: stringOrNull(row.kb_id),
    dimension: row.dimension as DgQualityRule['dimension'],
    severity: row.severity as DgQualitySeverity,
    checkType: row.check_type as DgQualityRule['checkType'],
    definition: recordOrEmpty(row.definition),
    threshold: recordOrEmpty(row.threshold),
    enabled: row.enabled === true || row.enabled === 1 ? 1 : 0,
    contractId: stringOrNull(row.contract_id),
    revision: numberOr(row.revision, 1),
    createdAt: stringOrNull(row.created_at),
    updatedAt: stringOrNull(row.updated_at),
  };
}

export function mapQualityRun(row: QualityRunRowRaw): DgQualityRun {
  return {
    id: String(row.id),
    dataSourceId: String(row.data_source_id ?? ''),
    trigger: String(row.trigger ?? 'manual'),
    status: row.status as DgQualityRun['status'],
    ruleIds: listOrEmpty(row.rule_ids).map(String),
    executor: stringOrNull(row.executor),
    dialect: String(row.dialect ?? 'tsql'),
    total: numberOr(row.total),
    passed: numberOr(row.passed),
    failed: numberOr(row.failed),
    error: numberOr(row.error),
    message: stringOrNull(row.message),
    startedAt: stringOrNull(row.started_at),
    finishedAt: stringOrNull(row.finished_at),
    createdAt: stringOrNull(row.created_at),
  };
}

export function mapQualityResult(row: QualityResultRowRaw): DgQualityResultItem {
  return {
    ruleId: String(row.rule_id),
    assetType: row.asset_type === 'table' ? 'table' : 'column',
    assetId: String(row.asset_id ?? ''),
    status: row.status as DgQualityResultItem['status'],
    metricValue: row.metric_value ?? null,
    message: stringOrNull(row.message),
    sample: Array.isArray(row.sample) ? row.sample : null,
    checkedAt: stringOrNull(row.checked_at),
  };
}

/** 规则快照：详情接口的 `rule_snapshot`，缺字段时保守兜底。 */
export function mapQualityRuleSnapshot(value: unknown): DgQualityRuleSnapshotItem[] {
  return listOrEmpty(value).map((item) => {
    const row = isRecord(item) ? item : {};
    return {
      ruleId: String(row.rule_id ?? ''),
      name: String(row.name ?? ''),
      assetType: row.asset_type === 'column' ? 'column' : 'table',
      assetId: String(row.asset_id ?? ''),
      severity: String(row.severity ?? 'warning') as DgQualitySeverity,
      sql: stringOrNull(row.sql),
    };
  });
}

export function mapQualityIssue(row: QualityIssueRowRaw): DgQualityIssue {
  return {
    id: String(row.id),
    ruleId: String(row.rule_id),
    assetType: row.asset_type === 'table' ? 'table' : 'column',
    assetId: String(row.asset_id ?? ''),
    dataSourceId: String(row.data_source_id ?? ''),
    severity: row.severity as DgQualitySeverity,
    status: row.status as DgQualityIssueStatus,
    assigneeId: stringOrNull(row.assignee_id),
    title: String(row.title ?? ''),
    detail: stringOrNull(row.detail),
    firstSeenAt: stringOrNull(row.first_seen_at),
    lastSeenAt: stringOrNull(row.last_seen_at),
    resolvedAt: stringOrNull(row.resolved_at),
    revision: numberOr(row.revision, 1),
    createdAt: stringOrNull(row.created_at),
    updatedAt: stringOrNull(row.updated_at),
  };
}

export function mapQualityIssueEvent(row: QualityIssueEventRowRaw): DgQualityIssueEvent {
  return {
    id: String(row.id),
    issueId: String(row.issue_id),
    eventType: String(row.event_type ?? ''),
    fromStatus: stringOrNull(row.from_status),
    toStatus: stringOrNull(row.to_status),
    actorId: stringOrNull(row.actor_id),
    note: stringOrNull(row.note),
    createdAt: stringOrNull(row.created_at),
  };
}

export function mapQualitySummaryItem(row: Record<string, unknown>): DgQualitySummaryItem {
  const open = recordOrEmpty(row.open_issues);
  const latest = row.latest_run;
  return {
    dataSourceId: String(row.data_source_id ?? ''),
    name: stringOrNull(row.name),
    totalRules: numberOr(row.total_rules),
    enabledRules: numberOr(row.enabled_rules),
    latestRun: isRecord(latest)
      ? {
          status: String(latest.status ?? ''),
          finishedAt: stringOrNull(latest.finished_at),
          passed: numberOr(latest.passed),
          failed: numberOr(latest.failed),
          error: numberOr(latest.error),
        }
      : null,
    openIssues: {
      critical: numberOr(open.critical),
      error: numberOr(open.error),
      warning: numberOr(open.warning),
      info: numberOr(open.info),
    },
  };
}

export function buildRuleListParams(params: DgQualityRuleListParams): Record<string, unknown> {
  return {
    ...(params.dataSourceId ? { data_source_id: params.dataSourceId } : {}),
    ...(params.assetType ? { asset_type: params.assetType } : {}),
    ...(params.assetId ? { asset_id: params.assetId } : {}),
    ...(params.dimension ? { dimension: params.dimension } : {}),
    ...(params.severity ? { severity: params.severity } : {}),
    ...(params.checkType ? { check_type: params.checkType } : {}),
    ...(params.enabled === 0 || params.enabled === 1 ? { enabled: params.enabled } : {}),
    ...(params.keyword ? { keyword: params.keyword } : {}),
    page: params.page,
    page_size: params.pageSize,
  };
}

export function buildRuleBody(body: DgQualityRuleBody): Record<string, unknown> {
  return {
    name: body.name,
    asset_type: body.assetType,
    asset_id: body.assetId,
    dimension: body.dimension,
    severity: body.severity,
    check_type: body.checkType,
    definition: body.definition,
    threshold: body.threshold,
    ...(body.enabled === 0 || body.enabled === 1 ? { enabled: body.enabled } : {}),
    ...(body.kbId ? { kb_id: body.kbId } : {}),
  };
}

export function buildRuleUpdateBody(body: DgQualityRuleUpdateBody): Record<string, unknown> {
  return {
    expected_revision: body.expectedRevision,
    ...(body.name !== undefined ? { name: body.name } : {}),
    ...(body.assetType !== undefined ? { asset_type: body.assetType } : {}),
    ...(body.assetId !== undefined ? { asset_id: body.assetId } : {}),
    ...(body.dimension !== undefined ? { dimension: body.dimension } : {}),
    ...(body.severity !== undefined ? { severity: body.severity } : {}),
    ...(body.checkType !== undefined ? { check_type: body.checkType } : {}),
    ...(body.definition !== undefined ? { definition: body.definition } : {}),
    ...(body.threshold !== undefined ? { threshold: body.threshold } : {}),
    ...(body.enabled === 0 || body.enabled === 1 ? { enabled: body.enabled } : {}),
    ...(body.kbId !== undefined ? { kb_id: body.kbId } : {}),
  };
}

export function buildRunListParams(params: DgQualityRunListParams): Record<string, unknown> {
  return {
    ...(params.dataSourceId ? { data_source_id: params.dataSourceId } : {}),
    ...(params.status ? { status: params.status } : {}),
    page: params.page,
    page_size: params.pageSize,
  };
}

export function buildIssueListParams(params: DgQualityIssueListParams): Record<string, unknown> {
  return {
    ...(params.status ? { status: params.status } : {}),
    ...(params.severity ? { severity: params.severity } : {}),
    ...(params.assigneeId ? { assignee_id: params.assigneeId } : {}),
    ...(params.dataSourceId ? { data_source_id: params.dataSourceId } : {}),
    ...(params.assetType ? { asset_type: params.assetType } : {}),
    ...(params.assetId ? { asset_id: params.assetId } : {}),
    ...(params.keyword ? { keyword: params.keyword } : {}),
    page: params.page,
    page_size: params.pageSize,
  };
}

function mapSqlItems(items: unknown): DgQualitySqlItem[] {
  return listOrEmpty(items).map((item) => {
    const row = isRecord(item) ? item : {};
    return {
      ruleId: String(row.rule_id ?? ''),
      name: String(row.name ?? ''),
      sql: stringOrNull(row.sql),
    };
  });
}

// ── 接口 ─────────────────────────────────────────────────────────────

export async function listQualityRules(
  params: DgQualityRuleListParams,
): Promise<DgQualityRuleListResult> {
  const data = await apiClient.get<{ items?: QualityRuleRowRaw[]; total?: number }>(
    `${BASE}/rules`, { params: buildRuleListParams(params) },
  );
  return {
    items: (data.items ?? []).map(mapQualityRule),
    total: numberOr(data.total),
  };
}

export async function getQualityRule(ruleId: string): Promise<DgQualityRule> {
  const data = await apiClient.get<QualityRuleRowRaw>(`${BASE}/rules/${ruleId}`);
  return mapQualityRule(data);
}

export async function createQualityRule(body: DgQualityRuleBody): Promise<DgQualityRule> {
  const data = await apiClient.post<QualityRuleRowRaw>(`${BASE}/rules`, buildRuleBody(body));
  return mapQualityRule(data);
}

export async function updateQualityRule(
  ruleId: string, body: DgQualityRuleUpdateBody,
): Promise<DgQualityRule> {
  const data = await apiClient.put<QualityRuleRowRaw>(
    `${BASE}/rules/${ruleId}`, buildRuleUpdateBody(body),
  );
  return mapQualityRule(data);
}

export async function deleteQualityRule(ruleId: string): Promise<void> {
  await apiClient.delete(`${BASE}/rules/${ruleId}`);
}

export async function importQualityRules(contractId: string): Promise<DgQualityRuleImportResult> {
  const data = await apiClient.post<{
    created?: number; skipped?: number; warnings?: unknown;
  }>(`${BASE}/rules/import`, { contract_id: contractId });
  return {
    created: numberOr(data.created),
    skipped: numberOr(data.skipped),
    warnings: listOrEmpty(data.warnings).map(String),
  };
}

export async function previewQualitySql(body: {
  dataSourceId: string; ruleIds?: string[]; dialect?: string;
}): Promise<DgQualityPreviewResult> {
  const data = await apiClient.post<{ rules?: unknown; warnings?: unknown }>(
    `${BASE}/preview-sql`,
    {
      data_source_id: body.dataSourceId,
      ...(body.ruleIds ? { rule_ids: body.ruleIds } : {}),
      ...(body.dialect ? { dialect: body.dialect } : {}),
    },
  );
  return {
    rules: mapSqlItems(data.rules),
    warnings: listOrEmpty(data.warnings).map(String),
  };
}

export async function listQualityRuns(
  params: DgQualityRunListParams,
): Promise<DgQualityRunListResult> {
  const data = await apiClient.get<{ items?: QualityRunRowRaw[]; total?: number }>(
    `${BASE}/runs`, { params: buildRunListParams(params) },
  );
  return {
    items: (data.items ?? []).map(mapQualityRun),
    total: numberOr(data.total),
  };
}

export async function createQualityRun(
  body: DgQualityRunCreateBody,
): Promise<DgQualityRunCreateResult> {
  const data = await apiClient.post<{
    run: QualityRunRowRaw; rules?: unknown; warnings?: unknown;
  }>(`${BASE}/runs`, {
    data_source_id: body.dataSourceId,
    ...(body.ruleIds ? { rule_ids: body.ruleIds } : {}),
    ...(body.trigger ? { trigger: body.trigger } : {}),
    ...(body.executor ? { executor: body.executor } : {}),
    ...(body.dialect ? { dialect: body.dialect } : {}),
  });
  return {
    run: mapQualityRun(data.run),
    rules: mapSqlItems(data.rules),
    warnings: listOrEmpty(data.warnings).map(String),
  };
}

export async function getQualityRun(runId: string): Promise<DgQualityRunDetail> {
  const data = await apiClient.get<QualityRunRowRaw & { results?: unknown }>(
    `${BASE}/runs/${runId}`,
  );
  return {
    ...mapQualityRun(data),
    results: listOrEmpty(data.results).map((item) =>
      mapQualityResult((isRecord(item) ? item : {}) as unknown as QualityResultRowRaw)),
    ruleSnapshot: mapQualityRuleSnapshot(data.rule_snapshot),
  };
}

export async function submitQualityRunResults(
  runId: string,
  body: { results: DgQualityResultSubmitItem[]; executor?: string; message?: string },
): Promise<DgQualityRunSubmitResult> {
  const data = await apiClient.post<{
    run: QualityRunRowRaw;
    counts?: { total?: number; passed?: number; failed?: number; error?: number };
  }>(`${BASE}/runs/${runId}/results`, {
    results: body.results.map((item) => ({
      rule_id: item.ruleId,
      status: item.status,
      ...(item.metricValue !== undefined ? { metric_value: item.metricValue } : {}),
      ...(item.message !== undefined ? { message: item.message } : {}),
      ...(item.sample !== undefined ? { sample: item.sample } : {}),
    })),
    ...(body.executor ? { executor: body.executor } : {}),
    ...(body.message ? { message: body.message } : {}),
  });
  return {
    run: mapQualityRun(data.run),
    counts: {
      total: numberOr(data.counts?.total),
      passed: numberOr(data.counts?.passed),
      failed: numberOr(data.counts?.failed),
      error: numberOr(data.counts?.error),
    },
  };
}

export async function failQualityRun(runId: string, message: string): Promise<DgQualityRun> {
  const data = await apiClient.post<QualityRunRowRaw>(
    `${BASE}/runs/${runId}/fail`, { message },
  );
  return mapQualityRun(data);
}

export async function listQualityIssues(
  params: DgQualityIssueListParams,
): Promise<DgQualityIssueListResult> {
  const data = await apiClient.get<{ items?: QualityIssueRowRaw[]; total?: number }>(
    `${BASE}/issues`, { params: buildIssueListParams(params) },
  );
  return {
    items: (data.items ?? []).map(mapQualityIssue),
    total: numberOr(data.total),
  };
}

export async function getQualityIssue(issueId: string): Promise<DgQualityIssueDetail> {
  const data = await apiClient.get<QualityIssueRowRaw & { events?: unknown }>(
    `${BASE}/issues/${issueId}`,
  );
  return {
    ...mapQualityIssue(data),
    events: listOrEmpty(data.events).map((item) =>
      mapQualityIssueEvent((isRecord(item) ? item : {}) as unknown as QualityIssueEventRowRaw)),
  };
}

export async function assignQualityIssue(
  issueId: string, assigneeId: string, expectedRevision: number,
): Promise<DgQualityIssue> {
  const data = await apiClient.post<QualityIssueRowRaw>(
    `${BASE}/issues/${issueId}/assign`,
    { assignee_id: assigneeId, expected_revision: expectedRevision },
  );
  return mapQualityIssue(data);
}

export async function updateQualityIssueStatus(
  issueId: string,
  body: { toStatus: DgQualityIssueStatus; expectedRevision: number; note?: string },
): Promise<DgQualityIssue> {
  const data = await apiClient.post<QualityIssueRowRaw>(
    `${BASE}/issues/${issueId}/status`,
    {
      to_status: body.toStatus,
      expected_revision: body.expectedRevision,
      ...(body.note ? { note: body.note } : {}),
    },
  );
  return mapQualityIssue(data);
}

export async function verifyQualityIssue(
  issueId: string,
  body: { passed: boolean; note?: string; expectedRevision: number },
): Promise<DgQualityIssue> {
  const data = await apiClient.post<QualityIssueRowRaw>(
    `${BASE}/issues/${issueId}/verify`,
    {
      passed: body.passed,
      expected_revision: body.expectedRevision,
      ...(body.note ? { note: body.note } : {}),
    },
  );
  return mapQualityIssue(data);
}

export async function commentQualityIssue(
  issueId: string, note: string,
): Promise<DgQualityIssueEvent> {
  const data = await apiClient.post<QualityIssueEventRowRaw>(
    `${BASE}/issues/${issueId}/comments`, { note },
  );
  return mapQualityIssueEvent(data);
}

/**
 * 全租户可建质量规则的数据库源（跨 KB 汇总，按 id 去重）。
 *
 * 质量规则绑定在数据库源上；这里复用合同页同一条取数链路
 * （KB 列表 → 各 KB 的 database 源），单个 KB 失败不拖垮其余 KB。
 */
export async function listQualityDataSourceOptions(): Promise<DgContractDataSourceOption[]> {
  try {
    const kbs = await listKbOptions();
    const groups = await Promise.all(
      kbs.map((kb) => listContractDataSources(kb.id).catch(() => [])),
    );
    const merged = new Map<string, DgContractDataSourceOption>();
    for (const option of groups.flat()) {
      if (!merged.has(option.id)) merged.set(option.id, option);
    }
    return [...merged.values()];
  } catch {
    return [];
  }
}

export async function getQualitySummary(dataSourceId?: string): Promise<DgQualitySummary> {
  const data = await apiClient.get<{
    data_sources?: unknown; totals?: Record<string, unknown>;
  }>(`${BASE}/summary`, {
    params: dataSourceId ? { data_source_id: dataSourceId } : {},
  });
  const totals = recordOrEmpty(data.totals);
  return {
    dataSources: listOrEmpty(data.data_sources).map((item) =>
      mapQualitySummaryItem(recordOrEmpty(item))),
    totals: {
      rules: numberOr(totals.rules),
      openIssues: numberOr(totals.open_issues),
      lastRunAt: stringOrNull(totals.last_run_at),
    },
  };
}
