/**
 * join 边提案客户端（join-agent 线）。
 *
 * 候选由后端确定性发现器产出（命名短名单 + 值包含采样），agent 只做复核；
 * 前端拿到的是「一条边一条提案」，确认/驳回各自独立。
 */
import { getData, postData, request } from './request';
import type { ApiResponse } from './request';

export type JoinProposalStatus = 'pending' | 'confirmed' | 'rejected';

export interface JoinProposal {
  proposalId: string;
  sourceEntity: string;
  sourceColumn: string;
  targetEntity: string;
  targetColumn: string;
  dataSourceId: string;
  status: JoinProposalStatus;
  confidence: 'high' | 'medium' | 'low';
  rationale: string;
  createdAt?: string | null;
  decidedAt?: string | null;
  decisionNote?: string;
}

function mapProposal(raw: any): JoinProposal {
  return {
    proposalId: String(raw?.proposal_id ?? ''),
    sourceEntity: String(raw?.source_entity ?? ''),
    sourceColumn: String(raw?.source_column ?? ''),
    targetEntity: String(raw?.target_entity ?? ''),
    targetColumn: String(raw?.target_column ?? ''),
    dataSourceId: String(raw?.data_source_id ?? ''),
    status: (raw?.status ?? 'pending') as JoinProposalStatus,
    confidence: (raw?.confidence ?? 'low') as JoinProposal['confidence'],
    rationale: String(raw?.rationale ?? ''),
    createdAt: raw?.created_at ?? null,
    decidedAt: raw?.decided_at ?? null,
    decisionNote: String(raw?.decision_note ?? ''),
  };
}

export async function listJoinProposals(
  kbId: string,
  status: JoinProposalStatus | '' = '',
): Promise<JoinProposal[]> {
  const res = request.get<ApiResponse<any>>(
    '/knowledge-base/ontology/join-proposals',
    { params: { knowledge_base_id: kbId, ...(status ? { status } : {}) } },
  );
  const body = await getData<any>(res);
  return (body?.proposals ?? []).map(mapProposal);
}

/** 跑一轮复核：确定性发现 + agent 复核，可能产出 0..N 条边。 */
export async function runJoinAgent(
  kbId: string,
  dataSourceId = '',
  task = '',
): Promise<{ ok: boolean; proposals: JoinProposal[]; candidates?: number; reason?: string; message?: string }> {
  const body = await getData<any>(
    request.post(
      '/knowledge-base/ontology/join-proposals/run',
      {
        knowledge_base_id: kbId,
        ...(dataSourceId ? { data_source_id: dataSourceId } : {}),
        ...(task ? { task } : {}),
      },
      { timeout: 180000 },
    ),
  );
  if (body?.ok === false) {
    return { ok: false, proposals: [], reason: body.reason, message: body.message };
  }
  return {
    ok: true,
    proposals: (body?.proposals ?? []).map((item: any) => mapProposal(item)),
    candidates: Number(body?.candidates ?? 0),
  };
}

export async function decideJoinProposal(
  kbId: string,
  proposalId: string,
  decision: 'confirmed' | 'rejected',
  note = '',
): Promise<{ changed: boolean; proposal: JoinProposal }> {
  // postData 已解包，不要再套 getData。
  const body = await postData<any>(
    `/knowledge-base/ontology/join-proposals/${proposalId}/decision`,
    { knowledge_base_id: kbId, decision, note },
  );
  return { changed: Boolean(body?.changed), proposal: mapProposal(body?.proposal) };
}
