/**
 * ② NL→SQL 已验证查询（verified query，N1-1）。
 *
 * 后端：`/api/v1/knowledge-base/verified-queries/*`。
 * 一个条目 = 一组高频问句 + 一份带参 SQL 模板；激活后命中的问句走快路径（不调 LLM）。
 * 所有写操作都带 `expected_version`（乐观锁，冲突时后端回 1003）。
 */
import { request, getData, postData } from './request';

export type VerifiedQueryStatus = 'draft' | 'active' | 'retired';
export type VerifiedParamType = 'string' | 'number' | 'date' | 'boolean' | 'enum';

export interface VerifiedQueryParam {
  name: string;
  type: VerifiedParamType;
  required?: boolean;
  description?: string;
  /** 从问句里取值的正则，必须含一个捕获组 */
  extract: string;
  enum?: string[];
}

export interface VerifiedQuery {
  id: string;
  knowledge_base_id: string;
  data_source_id: string;
  name: string;
  description: string;
  questions: string[];
  sql_template: string;
  params: VerifiedQueryParam[];
  status: VerifiedQueryStatus;
  version: number;
  created_by?: string | null;
  updated_by?: string | null;
  activated_by?: string | null;
  activated_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface VerifiedQuerySavePayload {
  knowledge_base_id: string;
  data_source_id: string;
  name: string;
  description?: string;
  questions: string[];
  sql_template: string;
  params: VerifiedQueryParam[];
  verified_query_id?: string;
  expected_version?: number;
}

export interface VerifiedQueryPreview {
  matched: boolean;
  reason?: string;
  id?: string;
  name?: string;
  params?: Record<string, string>;
  sql?: string;
  candidates?: { id: string; name: string }[];
}

export async function listVerifiedQueries(
  kbId: string,
  dataSourceId = '',
): Promise<{ items: VerifiedQuery[]; total: number }> {
  return getData<{ items: VerifiedQuery[]; total: number }>(
    request.get('/knowledge-base/verified-queries', {
      params: { knowledge_base_id: kbId, data_source_id: dataSourceId },
    }),
  );
}

export async function saveVerifiedQuery(
  payload: VerifiedQuerySavePayload,
): Promise<VerifiedQuery> {
  const { knowledge_base_id, verified_query_id, ...body } = payload;
  if (verified_query_id) {
    return getData<VerifiedQuery>(
      request.patch(`/knowledge-base/verified-queries/${verified_query_id}`, {
        ...body,
        knowledge_base_id,
      }),
    );
  }
  return postData<VerifiedQuery>('/knowledge-base/verified-queries', {
    ...body,
    knowledge_base_id,
  });
}

export async function activateVerifiedQuery(
  kbId: string,
  id: string,
  expectedVersion: number,
): Promise<VerifiedQuery> {
  return postData<VerifiedQuery>(
    `/knowledge-base/verified-queries/${id}/activate`,
    { knowledge_base_id: kbId, expected_version: expectedVersion },
  );
}

export async function retireVerifiedQuery(
  kbId: string,
  id: string,
  expectedVersion: number,
): Promise<VerifiedQuery> {
  return postData<VerifiedQuery>(
    `/knowledge-base/verified-queries/${id}/retire`,
    { knowledge_base_id: kbId, expected_version: expectedVersion },
  );
}

export async function deleteVerifiedQuery(
  kbId: string,
  id: string,
  expectedVersion: number,
): Promise<boolean> {
  const res = await getData<{ deleted: boolean }>(
    request.delete(`/knowledge-base/verified-queries/${id}`, {
      params: { knowledge_base_id: kbId, expected_version: expectedVersion },
    }),
  );
  return res?.deleted === true;
}

export async function previewVerifiedMatch(
  kbId: string,
  dataSourceId: string,
  question: string,
): Promise<VerifiedQueryPreview> {
  return postData<VerifiedQueryPreview>(
    '/knowledge-base/verified-queries/preview-match',
    { knowledge_base_id: kbId, data_source_id: dataSourceId, question },
  );
}
