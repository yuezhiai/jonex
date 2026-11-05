/**
 * 数据治理控制面 P1（#11）API。
 *
 * 后端：`/api/v1/knowledge-base/governance/*`。
 * `knowledge_base_id` 在读接口走 Query；update/delete 也走 Query（按 KB 收口）。
 */
import { request, getData, postData } from './request';

export type GovernanceLevel = 'public' | 'internal' | 'confidential' | 'secret';
export type GovernanceEffect = 'allow' | 'deny' | 'mask' | 'row_filter';
export type GovernanceDecision = 'allow' | 'deny' | 'mask' | 'row_filter';

export interface GovernanceLabel {
  id: string;
  data_source_id: string;
  table_name: string;
  column_name?: string | null;
  level: GovernanceLevel;
  tags: string[];
  origin: 'manual' | 'imported' | 'derived';
  note?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface GovernanceTag {
  id: string;
  name: string;
  color?: string | null;
  description?: string | null;
}

export interface GovernancePolicy {
  id: string;
  name: string;
  description?: string | null;
  effect: GovernanceEffect;
  priority: number;
  resource_type: 'table' | 'column' | 'tag';
  table_pattern?: string | null;
  column_pattern?: string | null;
  tag_name?: string | null;
  min_clearance: GovernanceLevel;
  mask_type?: 'null' | 'redact' | null;
  row_filter?: string | null;
  valid_from?: string | null;
  valid_to?: string | null;
  enabled: boolean;
  note?: string | null;
}

export interface GovernanceClearance {
  user_id: string;
  username?: string | null;
  display_name?: string | null;
  level: GovernanceLevel;
  granted_by?: string | null;
  note?: string | null;
}

export interface GovernanceAudit {
  id: string;
  user_id?: string | null;
  action: 'select' | 'predicate' | 'aggregate';
  resource_table?: string | null;
  resource_column?: string | null;
  level?: GovernanceLevel | null;
  decision: GovernanceDecision;
  policy_id?: string | null;
  reason?: string | null;
  created_at?: string | null;
}

export interface GovernanceEvaluation {
  effect: GovernanceDecision;
  policy_id?: string | null;
  reason?: string | null;
  mask_type?: string | null;
  row_filters: string[];
  level?: GovernanceLevel | null;
  clearance?: GovernanceLevel | null;
}

interface ListResult<T> { items: T[]; total: number }

// ── 标签 ─────────────────────────────────────────────────────────────
export async function listGovernanceLabels(
  kbId: string, dsId?: string, tableName?: string,
): Promise<ListResult<GovernanceLabel>> {
  return getData<ListResult<GovernanceLabel>>(request.get('/knowledge-base/governance/labels', {
    params: { knowledge_base_id: kbId, data_source_id: dsId, table_name: tableName },
  }));
}

export async function createGovernanceLabel(
  kbId: string, body: Partial<GovernanceLabel> & {
    data_source_id: string; table_name: string; level: GovernanceLevel;
  },
): Promise<GovernanceLabel> {
  return postData<GovernanceLabel>('/knowledge-base/governance/labels',
    { ...body, knowledge_base_id: kbId });
}

export async function updateGovernanceLabel(
  kbId: string, labelId: string, body: Partial<GovernanceLabel>,
): Promise<GovernanceLabel> {
  return getData<GovernanceLabel>(request.patch(
    `/knowledge-base/governance/labels/${labelId}`, body,
    { params: { knowledge_base_id: kbId } }));
}

export async function deleteGovernanceLabel(kbId: string, labelId: string): Promise<boolean> {
  const res = await getData<{ deleted: boolean }>(request.delete(
    `/knowledge-base/governance/labels/${labelId}`,
    { params: { knowledge_base_id: kbId } }));
  return res?.deleted === true;
}

// ── 敏感数据分布扫描（W2.7a）─────────────────────────────────────────
export type ScanConfidence = 'low' | 'medium' | 'high';

export interface SensitiveScanItem {
  data_source_id: string;
  table: string;
  column: string;
  matched: string[];
  suggested_level: GovernanceLevel;
  suggested_tags: string[];
  confidence: ScanConfidence;
  status: 'candidate' | 'labeled' | 'derived_present';
  current_level?: GovernanceLevel | null;
  current_tags?: string[];
  sample_masked?: string;
}

export interface SensitiveScanStats {
  sources: number;
  tables: number;
  columns: number;
  candidates: number;
  by_level: Record<string, number>;
  warnings: string[];
}

export interface SensitiveScanResult {
  items: SensitiveScanItem[];
  stats: SensitiveScanStats;
}

export interface SensitiveAdoptItem {
  data_source_id: string;
  table: string;
  column: string;
  level: GovernanceLevel;
  tags: string[];
  note?: string;
}

export interface SensitiveAdoptResult {
  created: number;
  skipped: number;
  items: Array<{ item: Record<string, unknown>; status: string; reason?: string }>;
}

export async function scanSensitiveData(
  kbId: string, body: { data_source_ids?: string[]; min_confidence?: ScanConfidence } = {},
): Promise<SensitiveScanResult> {
  return postData<SensitiveScanResult>('/knowledge-base/governance/sensitive-scan',
    { knowledge_base_id: kbId, ...body });
}

export async function adoptSensitiveScan(
  kbId: string, items: SensitiveAdoptItem[],
): Promise<SensitiveAdoptResult> {
  return postData<SensitiveAdoptResult>('/knowledge-base/governance/sensitive-scan/adopt',
    { knowledge_base_id: kbId, items });
}

// ── 标签字典 ─────────────────────────────────────────────────────────
export async function listGovernanceTags(kbId: string): Promise<ListResult<GovernanceTag>> {
  return getData<ListResult<GovernanceTag>>(request.get(
    '/knowledge-base/governance/tags', { params: { knowledge_base_id: kbId } }));
}

export async function createGovernanceTag(
  kbId: string, body: { name: string; color?: string; description?: string },
): Promise<GovernanceTag> {
  return postData<GovernanceTag>('/knowledge-base/governance/tags',
    { ...body, knowledge_base_id: kbId });
}

export async function updateGovernanceTag(
  kbId: string, tagId: string, body: Partial<GovernanceTag>,
): Promise<GovernanceTag> {
  return getData<GovernanceTag>(request.patch(
    `/knowledge-base/governance/tags/${tagId}`, body,
    { params: { knowledge_base_id: kbId } }));
}

export async function deleteGovernanceTag(kbId: string, tagId: string): Promise<boolean> {
  const res = await getData<{ deleted: boolean }>(request.delete(
    `/knowledge-base/governance/tags/${tagId}`,
    { params: { knowledge_base_id: kbId } }));
  return res?.deleted === true;
}

// ── 用户密级 ─────────────────────────────────────────────────────────
export async function listGovernanceClearance(kbId: string): Promise<ListResult<GovernanceClearance>> {
  return getData<ListResult<GovernanceClearance>>(request.get(
    '/knowledge-base/governance/clearance', { params: { knowledge_base_id: kbId } }));
}

export async function setGovernanceClearance(
  kbId: string, body: { user_id: string; level: GovernanceLevel; note?: string },
): Promise<GovernanceClearance> {
  return getData<GovernanceClearance>(request.put(
    '/knowledge-base/governance/clearance',
    { ...body, knowledge_base_id: kbId }));
}

// ── 策略 ─────────────────────────────────────────────────────────────
export async function listGovernancePolicies(kbId: string): Promise<ListResult<GovernancePolicy>> {
  return getData<ListResult<GovernancePolicy>>(request.get(
    '/knowledge-base/governance/policies', { params: { knowledge_base_id: kbId } }));
}

export async function createGovernancePolicy(
  kbId: string, body: Partial<GovernancePolicy> & { name: string; effect: GovernanceEffect },
): Promise<GovernancePolicy> {
  return postData<GovernancePolicy>('/knowledge-base/governance/policies',
    { ...body, knowledge_base_id: kbId });
}

export async function updateGovernancePolicy(
  kbId: string, policyId: string, body: Partial<GovernancePolicy>,
): Promise<GovernancePolicy> {
  return getData<GovernancePolicy>(request.patch(
    `/knowledge-base/governance/policies/${policyId}`, body,
    { params: { knowledge_base_id: kbId } }));
}

export async function deleteGovernancePolicy(kbId: string, policyId: string): Promise<boolean> {
  const res = await getData<{ deleted: boolean }>(request.delete(
    `/knowledge-base/governance/policies/${policyId}`,
    { params: { knowledge_base_id: kbId } }));
  return res?.deleted === true;
}

// ── 审计 / 试算 ──────────────────────────────────────────────────────
export async function listGovernanceAudit(
  kbId: string, params: { decision?: string; action?: string; offset?: number; limit?: number } = {},
): Promise<ListResult<GovernanceAudit>> {
  return getData<ListResult<GovernanceAudit>>(request.get('/knowledge-base/governance/audit', {
    params: { knowledge_base_id: kbId, ...params },
  }));
}

export async function evaluateGovernance(
  kbId: string,
  body: { data_source_id: string; table: string; column?: string; action?: string },
): Promise<GovernanceEvaluation> {
  return postData<GovernanceEvaluation>('/knowledge-base/governance/evaluate',
    { ...body, knowledge_base_id: kbId });
}

// ── 指标调用审批（W4.8）──────────────────────────────────────────────
export type MetricCallApprovalStatus = 'pending' | 'approved' | 'suspended';

export interface MetricCallApproval {
  id: string;
  knowledge_base_id: string;
  metric_name: string;
  status: MetricCallApprovalStatus;
  requested_by?: string | null;
  decided_by?: string | null;
  reason?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface MetricCallApprovalList {
  items: MetricCallApproval[];
}

export async function listMetricCallApprovals(
  kbId: string, status?: MetricCallApprovalStatus,
): Promise<MetricCallApprovalList> {
  return getData<MetricCallApprovalList>(request.get(
    '/knowledge-base/metrics/approvals',
    { params: { knowledge_base_id: kbId, status } }));
}

export async function requestMetricCallApproval(
  kbId: string, metricName: string, reason?: string,
): Promise<{ item: MetricCallApproval; created: boolean }> {
  return postData<{ item: MetricCallApproval; created: boolean }>('/knowledge-base/metrics/approvals/request', {
    knowledge_base_id: kbId, metric_name: metricName, reason,
  });
}

export async function approveMetricCall(
  kbId: string, metricName: string, reason?: string,
): Promise<{ item: MetricCallApproval; status: string }> {
  return postData<{ item: MetricCallApproval; status: string }>('/knowledge-base/metrics/approvals/approve', {
    knowledge_base_id: kbId, metric_name: metricName, reason,
  });
}

export async function suspendMetricCall(
  kbId: string, metricName: string, reason?: string,
): Promise<{ item: MetricCallApproval; status: string }> {
  return postData<{ item: MetricCallApproval; status: string }>('/knowledge-base/metrics/approvals/suspend', {
    knowledge_base_id: kbId, metric_name: metricName, reason,
  });
}
