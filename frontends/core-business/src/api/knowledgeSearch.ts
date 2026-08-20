import { readAccessToken } from '@jonex/shell-sdk';
import { request, getData } from './request';
import {
  listMockKnowledgeSearchHistory,
  saveMockKnowledgeSearchHistory,
  deleteMockKnowledgeSearchHistory,
  clearMockKnowledgeSearchHistory,
} from '../mocks/knowledgeSearchHistoryStore';
import type {
  KnowledgeReference,
  KnowledgeSearchDomain,
  KnowledgeSearchHistoryItem,
  ReasoningTrace,
  KnowledgeSearchMode,
  KnowledgeSearchOverview,
  KnowledgeSearchStrictConfig,
  KnowledgeSearchStreamHandlers,
  KnowledgeSearchStreamMeta,
  KnowledgeSearchStreamParams,
  SaveKnowledgeSearchHistoryPayload,
  SearchFeedbackType,
  CancelSearchFeedbackParams,
  SubmitSearchFeedbackParams,
  SubmitSearchFeedbackResponse,
} from '../types/knowledgeSearch';

type NormalizedSearchParams = {
  query: string;
  mode: KnowledgeSearchMode;
  topK: number;
  domainId: string;
  kbIds: string[];
  strictConfig?: KnowledgeSearchStrictConfig;
};

interface RawKnowledgeSearchHistoryItem {
  id: string;
  query: string;
  searched_at?: string;
  searchedAt?: string;
  result_count?: number;
  resultCount?: number;
  domain?: string;
  domain_id?: string;
  domainId?: string;
  domain_space_id?: string;
  domainSpaceId?: string;
  status?: KnowledgeSearchHistoryItem['status'];
  answer_preview?: string;
  answerPreview?: string;
  reference_count?: number;
  referenceCount?: number;
  duration_ms?: number;
  durationMs?: number;
  mode?: string;
  top_k?: number;
  topK?: number;
  answer?: string;
  references?: KnowledgeReference[];
  reasoning?: ReasoningTrace | null;
  strictConfig?: KnowledgeSearchStrictConfig | null;
  metadata?: {
    domain?: string;
    domain_id?: string;
    domainId?: string;
    strict_config?: KnowledgeSearchStrictConfig | null;
  };
}

function normalizeSearchParams(params: KnowledgeSearchStreamParams): NormalizedSearchParams {
  return {
    query: params.query,
    mode: params.mode ?? 'mix',
    topK: params.topK ?? 5,
    domainId: params.domainId ?? 'all',
    kbIds: params.kbIds ?? [],
    strictConfig: params.strictConfig,
  };
}

function normalizeHistoryItem(item: RawKnowledgeSearchHistoryItem): KnowledgeSearchHistoryItem {
  const metadata = item.metadata ?? {};
  return {
    id: item.id,
    query: item.query,
    searchedAt: item.searchedAt ?? item.searched_at ?? new Date().toISOString(),
    resultCount: item.resultCount ?? item.result_count ?? 0,
    domain: item.domain ?? metadata.domain,
    domainId: item.domainId ?? item.domain_id ?? metadata.domainId ?? metadata.domain_id,
    domainSpaceId: item.domainSpaceId ?? item.domain_space_id ?? undefined,
    status: item.status,
    answerPreview: item.answerPreview ?? item.answer_preview,
    referenceCount: item.referenceCount ?? item.reference_count,
    durationMs: item.durationMs ?? item.duration_ms,
    mode: item.mode === 'hybrid' ? 'hybrid' : undefined,
    topK: item.topK ?? item.top_k,
    answer: item.answer,
    references: item.references,
    reasoning: item.reasoning,
    strictConfig: item.strictConfig ?? (metadata.strict_config as KnowledgeSearchStrictConfig | null | undefined),
  };
}

/** 严格模式默认配置（普通 / 深度检索共用前 5 项，深度额外含后 2 项） */
export const DEFAULT_STRICT_CONFIG: KnowledgeSearchStrictConfig = {
  strict_mode: false,
  strict_max_attempts: 3,
  strict_min_score: 0.8,
  strict_require_reference: true,
  strict_require_grounded: true,
  max_subqueries: 6,
  allow_common_sense: true,
};

export const DEFAULT_FAST_STRICT_CONFIG: KnowledgeSearchStrictConfig = {
  strict_mode: true,
  strict_max_attempts: 1,
  strict_min_score: 0.7,
  strict_require_reference: true,
  strict_require_grounded: true,
  max_subqueries: 3,
  allow_common_sense: false,
};

export const DEFAULT_DEEP_STRICT_CONFIG: KnowledgeSearchStrictConfig = {
  strict_mode: true,
  strict_max_attempts: 3,
  strict_min_score: 0.8,
  strict_require_reference: true,
  strict_require_grounded: true,
  max_subqueries: 6,
  allow_common_sense: true,
};

export async function getKnowledgeSearchOverview(): Promise<KnowledgeSearchOverview> {
  // if (useMock) return mockKnowledgeSearchOverview
  return getData<KnowledgeSearchOverview>(request.get('/knowledge-base/search/overview'));
}

export async function getKnowledgeSearchDomains(spaceId?: string): Promise<KnowledgeSearchDomain[]> {
  const params: Record<string, string | number> = { limit: 100 };
  if (spaceId) params.space_id = spaceId;
  const result = await getData<{ items: KnowledgeSearchDomain[] }>(request.get('/knowledge-base/services', { params }));
  const items = result.items ?? [];
  return [{ id: 'all', name: '', description: '' }, ...items];
}

/** 检索历史分页查询：按时间倒序，返回列表与总数（支持"查看更多"翻页） */
export async function getKnowledgeSearchHistory(
  knowledgeBaseId: string,
  domainSpaceId?: string,
  page: number = 1,
  pageSize: number = 20,
): Promise<{ items: KnowledgeSearchHistoryItem[]; total: number }> {
  // if (useMock) return { items: listMockKnowledgeSearchHistory(), total: ... }
  const params: Record<string, string | number> = { knowledge_base_id: knowledgeBaseId, page, page_size: pageSize };
  if (domainSpaceId) params.domain_space_id = domainSpaceId;
  const result = await getData<{ items: RawKnowledgeSearchHistoryItem[]; total: number }>(
    request.get('/knowledge-base/search/history', { params }),
  );
  return { items: (result.items ?? []).map(normalizeHistoryItem), total: result.total ?? 0 };
}

/** [jonex] 历史快照引用重新富化：按 doc_id/locations 重新生成预签名 URL（快照里 raw_url 会过期，展示时调用） */
export async function resolveKnowledgeReferences(
  refs: KnowledgeReference[],
): Promise<KnowledgeReference[]> {
  if (!refs.length) return [];
  const parsed: Array<Record<string, unknown>> = [];
  for (const r of refs) {
    if (!r.doc_id) continue;
    const locs = r.locations?.length ? r.locations : [undefined];
    for (const loc of locs) {
      const row = loc as { row_start?: number; row_end?: number; table_idx?: number } | undefined;
      parsed.push({
        doc_id: r.doc_id,
        kb_id: r.kb_id ?? undefined,
        chunk_index: loc?.chunk_index ?? undefined,
        char_start: loc?.char_start ?? undefined,
        char_end: loc?.char_end ?? undefined,
        page_no: loc?.page_no ?? undefined,
        time_start: loc?.time_start ?? undefined,
        time_end: loc?.time_end ?? undefined,
        row_start: row?.row_start ?? undefined,
        row_end: row?.row_end ?? undefined,
        table_idx: row?.table_idx ?? undefined,
      });
    }
  }
  if (!parsed.length) return [];
  const result = await getData<{ references?: KnowledgeReference[] }>(
    request.post('/knowledge-base/documents/references/resolve', { refs: parsed }),
  );
  return result.references ?? [];
}

export async function saveKnowledgeSearchHistory(
  knowledgeBaseId: string,
  payload: SaveKnowledgeSearchHistoryPayload,
): Promise<KnowledgeSearchHistoryItem> {
  // if (useMock) {
  //   const updated = saveMockKnowledgeSearchHistory(payload)
  //   return updated[0]
  // }
  const result = await getData<RawKnowledgeSearchHistoryItem | { item: RawKnowledgeSearchHistoryItem }>(
    request.post('/knowledge-base/search/history', { ...payload, knowledge_base_id: knowledgeBaseId }),
  );
  return normalizeHistoryItem(('item' in result ? result.item : result) as RawKnowledgeSearchHistoryItem);
}

export async function deleteKnowledgeSearchHistory(knowledgeBaseId: string, id: string): Promise<void> {
  // if (useMock) {
  //   deleteMockKnowledgeSearchHistory(id)
  //   return
  // }
  await getData(
    request.delete(`/knowledge-base/search/history/${id}`, { params: { knowledge_base_id: knowledgeBaseId } }),
  );
}

export async function clearKnowledgeSearchHistory(knowledgeBaseId: string): Promise<void> {
  // if (useMock) {
  //   clearMockKnowledgeSearchHistory()
  //   return
  // }
  await getData(request.delete('/knowledge-base/search/history', { params: { knowledge_base_id: knowledgeBaseId } }));
}

export async function streamKnowledgeSearch(
  params: KnowledgeSearchStreamParams,
  handlers: KnowledgeSearchStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const normalized = normalizeSearchParams(params);

  const token = readAccessToken();
  const baseUrl = (import.meta as any).env?.VITE_API_BASE_URL || '/api/v1';
  // 快速检索走混合管线统一入口 /search/mix（按 kb_type 分流 lightrag / openkb）；
  // 深度检索仍走 /search/deep（暂不支持 openkb，见 docs/openkb/llmwiki-reasoning）
  const endpoint = params.deep ? '/knowledge-base/search/deep' : '/knowledge-base/search/mix';
  const url = new URL(`${baseUrl}${endpoint}`, window.location.origin);

  const deep = params.deep === true;
  const strict = normalized.strictConfig;
  const body: Record<string, unknown> = {
    query: normalized.query,
    mode: normalized.mode,
    top_k: normalized.topK,
    with_reasoning: true,
  };
  if (strict) {
    // 严格模式参数（普通 / 深度检索共用）
    body.strict_mode = strict.strict_mode;
    body.strict_max_attempts = strict.strict_max_attempts;
    body.strict_min_score = strict.strict_min_score;
    body.strict_require_reference = strict.strict_require_reference;
    body.strict_require_grounded = strict.strict_require_grounded;
    if (deep) {
      // 深度检索额外配置
      body.max_subqueries = strict.max_subqueries;
      body.allow_common_sense = strict.allow_common_sense;
    }
  }
  if (normalized.domainId && normalized.domainId !== 'all') {
    body.domain_id = normalized.domainId;
  }
  if (normalized.kbIds.length > 0) {
    body.knowledge_base_ids = normalized.kbIds;
  }

  const response = await fetch(url.toString(), {
    method: 'POST',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
    signal,
  });

  if (response.status === 401) {
    handlers.onError?.(new Error('Session expired, please log in again'));
    return;
  }

  if (!response.ok) {
    throw new Error(`Search failed: ${response.status}`);
  }

  try {
    const result = await response.json();

    if (!result.success || !result.data?.answer) {
      handlers.onDelta('Sorry, no relevant knowledge found.', { source: result.data?.source });
      handlers.onDone?.();
      return;
    }

    // 模拟流式输出：逐字输出 answer 模拟打字效果
    const answer = result.data.answer;
    const meta = {
      source: result.data.source,
      references: result.data.references ?? [],
      reasoning: result.data.reasoning ?? null,
      rag_used: result.data.rag_used,
    };
    for (let i = 0; i < answer.length; i += 2) {
      if (signal?.aborted) return;
      handlers.onDelta(answer.slice(i, i + 2), meta);
      await new Promise<void>((resolve) => setTimeout(resolve, 25));
    }
    handlers.onDone?.(meta);
  } catch (error) {
    if (signal?.aborted) return;
    handlers.onError?.(error instanceof Error ? error : new Error('Knowledge search failed, please retry'));
  }
}

/** 提交对当前搜索回答的「有帮助/无帮助」反馈 */
export async function submitSearchFeedback(params: SubmitSearchFeedbackParams): Promise<SubmitSearchFeedbackResponse> {
  const token = readAccessToken();
  const response = await fetch('/api/v1/knowledge-base/search/feedback', {
    method: 'POST',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      query: params.query,
      session_id: params.sessionId,
      answer_preview: params.answerPreview,
      feedback_type: params.feedbackType,
      knowledge_base_ids: params.kbIds,
      searched_at: params.searchedAt,
    }),
  });
  if (!response.ok) {
    throw new Error('Failed to submit feedback');
  }
  const result = await response.json();
  return result.data ?? { success: true, feedbackType: params.feedbackType, likeCount: 1, dislikeCount: 0 };
}

/** 取消对当前搜索回答的反馈（再次点击切换状态） */
export async function cancelSearchFeedback(params: CancelSearchFeedbackParams): Promise<SubmitSearchFeedbackResponse> {
  const token = readAccessToken();
  const response = await fetch('/api/v1/knowledge-base/search/feedback', {
    method: 'DELETE',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      session_id: params.sessionId,
      feedback_type: params.feedbackType,
      knowledge_base_ids: params.kbIds,
    }),
  });
  if (!response.ok) {
    throw new Error('Failed to cancel feedback');
  }
  const result = await response.json();
  return result.data ?? { success: true, feedbackType: params.feedbackType, likeCount: 0, dislikeCount: 0 };
}

// ── 情况追踪（搜索反馈管理）API ──────────────────────────────

/** 查询知识库的搜索反馈列表 */
export async function getSearchFeedbackList(
  knowledgeBaseId: string,
  params?: { feedbackType?: SearchFeedbackType; page?: number; pageSize?: number },
): Promise<import('../types/knowledgeSearch').SearchFeedbackListResponse> {
  const token = readAccessToken();
  const searchParams = new URLSearchParams({ knowledge_base_id: knowledgeBaseId });
  if (params?.feedbackType) searchParams.set('feedback_type', params.feedbackType);
  if (params?.page) searchParams.set('page', String(params.page));
  if (params?.pageSize) searchParams.set('page_size', String(params.pageSize));

  const response = await fetch(`/api/v1/knowledge-base/search/feedback?${searchParams}`, {
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!response.ok) throw new Error('Failed to fetch feedback list');
  const result = await response.json();
  return result.data ?? { items: [], total: 0, like_count: 0, dislike_count: 0, page: 1, page_size: 50 };
}

/** 切换反馈采纳状态 */
export async function toggleSearchFeedbackAdopted(feedbackId: string): Promise<void> {
  const token = readAccessToken();
  const response = await fetch('/api/v1/knowledge-base/search/feedback/toggle-adopt', {
    method: 'POST',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ feedback_id: feedbackId }),
  });
  if (!response.ok) throw new Error('Failed to toggle feedback status');
}

/** 获取知识库的反馈统计 */
export async function getSearchFeedbackStats(
  knowledgeBaseId: string,
): Promise<import('../types/knowledgeSearch').SearchFeedbackStats> {
  const token = readAccessToken();
  const response = await fetch(`/api/v1/knowledge-base/search/feedback/stats?knowledge_base_id=${knowledgeBaseId}`, {
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!response.ok) throw new Error('Failed to fetch feedback statistics');
  const result = await response.json();
  return result.data ?? { total: 0, like_count: 0, dislike_count: 0 };
}
