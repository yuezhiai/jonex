/**
 * 指标提案客户端（metric-agent 线）。
 *
 * agent 一次构建 30–120 秒：`buildMetricProposal` 的 axios 超时按 180s 配，
 * 与后端 `METRIC_AGENT_TIMEOUT_S`（默认 150s）留出余量。产出**只是提案**：
 * 确认/驳回前不影响任何检索或问数。
 */
import { getData, postData, request } from './request';
import type { ApiResponse } from './request';

export type MetricProposalStatus = 'pending' | 'confirmed' | 'rejected';
export type MetricConfidence = 'high' | 'medium' | 'low';

export interface MetricVerificationCheck {
  name: string;
  status: 'pass' | 'warn' | 'fail';
  detail: string;
}

export interface MetricVerification {
  ok: boolean;
  value?: number | null;
  checks: MetricVerificationCheck[];
  warnings: string[];
}

export interface MetricProposal {
  proposalId: string;
  name: string;
  status: MetricProposalStatus;
  confidence: MetricConfidence;
  formulaSql: string;
  grain: string;
  filters: string;
  rationale: string;
  dataSourceId: string;
  steps: number;
  usage?: Record<string, number | null>;
  verification?: MetricVerification | null;
  createdAt?: string | null;
  createdBy?: string;
  decidedBy?: string;
  decidedAt?: string | null;
  decisionNote?: string;
}

function mapProposal(raw: any): MetricProposal {
  const verification = raw?.verification;
  return {
    proposalId: String(raw?.proposal_id ?? ''),
    name: String(raw?.name ?? ''),
    status: (raw?.status ?? 'pending') as MetricProposalStatus,
    confidence: (raw?.confidence ?? 'low') as MetricConfidence,
    formulaSql: String(raw?.formula_sql ?? ''),
    grain: String(raw?.grain ?? ''),
    filters: String(raw?.filters ?? ''),
    rationale: String(raw?.rationale ?? ''),
    dataSourceId: String(raw?.data_source_id ?? ''),
    steps: Number(raw?.steps ?? 0),
    usage: raw?.usage ?? undefined,
    verification: verification
      ? {
          ok: Boolean(verification.ok),
          value: verification.value ?? null,
          checks: Array.isArray(verification.checks) ? verification.checks : [],
          warnings: Array.isArray(verification.warnings) ? verification.warnings : [],
        }
      : null,
    createdAt: raw?.created_at ?? null,
    createdBy: String(raw?.created_by ?? ''),
    decidedBy: String(raw?.decided_by ?? ''),
    decidedAt: raw?.decided_at ?? null,
    decisionNote: String(raw?.decision_note ?? ''),
  };
}

export async function listMetricProposals(
  kbId: string,
  status: MetricProposalStatus | '' = '',
): Promise<MetricProposal[]> {
  const res = request.get<ApiResponse<any>>(
    '/knowledge-base/ontology/metric-proposals',
    { params: { knowledge_base_id: kbId, ...(status ? { status } : {}) } },
  );
  const body = await getData<any>(res);
  return (body?.proposals ?? []).map(mapProposal);
}

/** 发起一次 agent 构建；失败时后端回 `{ok:false, reason, message}`。 */
export async function buildMetricProposal(
  kbId: string,
  task: string,
  dataSourceId = '',
): Promise<{ ok: boolean; proposal?: MetricProposal; reason?: string; message?: string }> {
  const body = await getData<any>(
    request.post(
      '/knowledge-base/ontology/metric-proposals',
      {
        knowledge_base_id: kbId,
        task,
        ...(dataSourceId ? { data_source_id: dataSourceId } : {}),
      },
      { timeout: 180000 },
    ),
  );
  if (body?.ok === false) {
    return {
      ok: false,
      reason: body.reason,
      message: body.message,
    };
  }
  return { ok: true, proposal: mapProposal(body) };
}

export async function decideMetricProposal(
  kbId: string,
  proposalId: string,
  decision: 'confirmed' | 'rejected',
  note = '',
): Promise<{ changed: boolean; proposal: MetricProposal }> {
  const body = await getData<any>(
    postData(
      `/knowledge-base/ontology/metric-proposals/${proposalId}/decision`,
      { knowledge_base_id: kbId, decision, note },
    ),
  );
  return { changed: Boolean(body?.changed), proposal: mapProposal(body?.proposal) };
}
