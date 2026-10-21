import { consumeWikiSearchStream } from './wikiSearchStream';
import { readAccessToken } from '@jonex/shell-sdk';
import { request, getData } from './request';
import { getDomainKnowledgeSpaces, getDomainKnowledgeList } from './domainKnowledge';
import type { DomainServiceItem } from '../types/domainService';
import type { KnowledgeBaseType } from '../types/domainKnowledge';
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
  SubmitAnswerFeedbackParams,
  SubmitAnswerFeedbackResponse,
  AnswerFeedbackEchoResponse,
  AnswerFeedbackListResponse,
  AnswerFeedbackStats,
  ImageQueryResult,
} from '../types/knowledgeSearch';

type NormalizedSearchParams = {
  query: string;
  mode: KnowledgeSearchMode;
  topK: number;
  domainId: string;
  kbIds: string[];
  kbTypes: KnowledgeBaseType[];
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
    /** [jonex] 以图问知识：用户原话（`query` 存的是识别产出的检索文本） */
    image_question?: string;
  };
  /** [jonex] 以图问知识：这条历史附带的查询图片（后端已按顺序返回） */
  query_images?: RawQueryImageRef[];
}

interface RawQueryImageRef {
  id: string;
  mime_type?: string;
  mimeType?: string;
  file_size?: number;
  fileSize?: number;
  view_url?: string | null;
  viewUrl?: string | null;
}

/** 后端识别的回包（snake_case；与 `ImageQueryResponse` 对齐） */
interface RawImageQueryResponse {
  query: string;
  images?: Array<{
    index: number;
    status: string;
    error_code?: number | null;
    image_id?: string | null;
    description?: string;
    ocr_text?: string;
    entities?: string[];
    cached?: boolean;
    persisted?: boolean;
  }>;
  entities?: string[];
  model?: string;
  partial?: boolean;
  duration_ms?: number;
  limits?: {
    max_images?: number;
    max_bytes?: number;
    max_total_bytes?: number;
  };
}

function normalizeSearchParams(params: KnowledgeSearchStreamParams): NormalizedSearchParams {
  return {
    query: params.query,
    mode: params.mode ?? 'mix',
    topK: params.topK ?? 5,
    domainId: params.domainId ?? '',
    kbIds: params.kbIds ?? [],
    kbTypes: params.kbTypes ?? [],
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
    // [jonex] 以图问知识：缺字段时给空数组（调用方不必判 undefined）
    queryImages: (item.query_images ?? []).map((image) => ({
      id: image.id,
      mimeType: image.mimeType ?? image.mime_type ?? 'image/jpeg',
      fileSize: image.fileSize ?? image.file_size ?? 0,
      viewUrl: image.viewUrl ?? image.view_url ?? null,
    })),
    imageQuestion: metadata.image_question,
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
  strict_mode: false,
  strict_max_attempts: 1,
  strict_min_score: 0.7,
  strict_require_reference: true,
  strict_require_grounded: true,
  max_subqueries: 3,
  allow_common_sense: false,
};

export const DEFAULT_DEEP_STRICT_CONFIG: KnowledgeSearchStrictConfig = {
  strict_mode: false,
  strict_max_attempts: 3,
  strict_min_score: 0.8,
  strict_require_reference: true,
  strict_require_grounded: true,
  max_subqueries: 6,
  allow_common_sense: true,
};

/**
 * [jonex] Wiki 检索的严格模式默认值（与快速检索一致）。
 * 注意：/search/jonex_wiki 端点的契约是「只收 query/mode/top_k/knowledge_base_ids」，
 * 严格模式参数不会下发（见 streamKnowledgeSearch 的 wikiEndpoint 判定），
 * 该配置仅用于面板切换时保持各档位的参数快照一致。
 */
export const DEFAULT_WIKI_STRICT_CONFIG: KnowledgeSearchStrictConfig = {
  ...DEFAULT_FAST_STRICT_CONFIG,
};

export async function getKnowledgeSearchOverview(): Promise<KnowledgeSearchOverview> {
  // if (useMock) return mockKnowledgeSearchOverview
  return getData<KnowledgeSearchOverview>(request.get('/knowledge-base/search/overview'));
}

export async function getKnowledgeSearchDomains(spaceId?: string): Promise<KnowledgeSearchDomain[]> {
  // [jonex] 检索维度 = 当前领域空间 + 领域服务（双维度并存）：
  // - 空间：只保留「当前领域空间」一项（跟随全局空间切换器），兜底覆盖该空间全部 KB
  //   （含未挂服务的新建 KB）；
  // - 服务：scope="search" 只返回「有可访问 KB 的服务」，且仅保留当前领域空间下的服务，
  //   不显示其他空间下的服务。
  // ⚠️ 本函数是**检索面**：KB 列表必须传 scope="search"（检索口径 = 可见空间 ∪ 授权 KB）。
  //    若用默认的列表口径（我管的空间 ∪ 授权 KB），纯 space member 会拿到 0 个 KB，
  //    空间维度随之变空、快速检索直接 return 发不出请求。**不要**把它改回默认。
  const [spaces, kbResult, servicesResult] = await Promise.all([
    getDomainKnowledgeSpaces(),
    getDomainKnowledgeList({ page: 1, pageSize: 100, scope: 'search' }),
    getData<{ items: DomainServiceItem[] }>(
      request.get('/knowledge-base/services', { params: { limit: 100, scope: 'search' } }),
    ),
  ]);

  // [jonex] 除按空间分桶外，另建 kb_id → kb_type 索引：领域服务的 kb_ids 只有 id，
  // 端点选择需要它们的引擎类型（jonex_wiki / openkb / lightrag）。
  const kbBySpace = new Map<string, { id: string; name: string; kbType?: KnowledgeBaseType }[]>();
  const kbTypeById = new Map<string, KnowledgeBaseType>();
  kbResult.list.forEach((kb) => {
    const bucket = kbBySpace.get(kb.spaceId) ?? [];
    bucket.push({ id: kb.id, name: kb.name, kbType: kb.kbType });
    kbBySpace.set(kb.spaceId, bucket);
    if (kb.kbType) kbTypeById.set(kb.id, kb.kbType);
  });

  // 空间维度只生成「当前领域空间」一项，不列出所有空间（产品：空间是默认范围，非可枚举筛选）
  const spaceItems: KnowledgeSearchDomain[] = (() => {
    if (!spaceId) return [];
    const currentSpace = spaces.find((s) => s.id === spaceId);
    if (!currentSpace) return [];
    const kbs = kbBySpace.get(currentSpace.id) ?? [];
    return [
      {
        id: currentSpace.id,
        name: currentSpace.name,
        kind: 'space',
        space_id: currentSpace.id,
        kb_ids: kbs.map((k) => k.id),
        kb_names: kbs.map((k) => k.name),
        // 空间下的 KB 类型可能缺失（列表项 kbType 是可选的）：只收已知的，
        // 长度不等即视为「类型不全」，端点选择回落 /search/mix
        kb_types: kbs.map((k) => k.kbType).filter((x): x is KnowledgeBaseType => !!x),
      },
    ];
  })();

  const serviceItems: KnowledgeSearchDomain[] = (servicesResult.items ?? [])
    // [jonex] 只保留当前领域空间下的服务，不显示其他空间下的服务
    .filter((svc) => !!spaceId && svc.space_id === spaceId)
    .map((svc) => ({
      id: svc.id,
      name: svc.name,
      kind: 'service',
      space_id: svc.space_id,
      space_name: svc.space_name,
      kb_ids: svc.kb_ids ?? [],
      kb_names: svc.kb_names ?? [],
      // 类型未知的 kb 不塞占位值：长度不等会被端点选择逻辑判为「类型不全」并回落 /search/mix
      kb_types: (svc.kb_ids ?? [])
        .map((id) => kbTypeById.get(id))
        .filter((x): x is KnowledgeBaseType => !!x),
    }));

  return [...spaceItems, ...serviceItems];
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

/**
 * [jonex] 按 id 单取一条历史的**快照**（answer / references / reasoning）。
 *
 * 列表接口默认不再返回快照 —— 实测 18 条历史 222 KB 里 **83% 是
 * `references[].locations[].text`（整段 chunk 正文）**，而侧栏只渲染
 * query / resultCount / 时间，一个字都用不上。点击某条历史要回放时才取这一条。
 *
 * 取不到（老数据没有快照 / 已被清理）返回 `null`，由调用方回退为「重新检索」。
 */
export async function getKnowledgeSearchHistorySnapshot(
  historyId: string,
): Promise<KnowledgeSearchHistoryItem | null> {
  const result = await getData<{ items: RawKnowledgeSearchHistoryItem[] }>(
    request.get('/knowledge-base/search/history', {
      params: { history_id: historyId, page: 1, page_size: 1 },
    }),
  );
  const item = result.items?.[0];
  return item ? normalizeHistoryItem(item) : null;
}

/**
 * [jonex] 历史快照引用补预签名 URL：快照里 raw_url / asset_url 会过期（COS 约 1 小时），
 * 展示前按 doc_id / image_idx 从 resolve 端点取新的补回快照（点击历史时调用）。
 *
 * ⚠️ 是「补 URL」不是「重取引用」：resolve 侧拿不回原文（`text` 不在 ParsedRef 契约里），
 * 且回传后再按 doc 聚合会把多条 location 塌成一条。整个替换快照 = 引用区闪一下就没，
 * 所以这里以快照为底、只往里填 URL，恢复不到的字段一律保留快照取值。
 */
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
        // [jonex] §image-refs B2：回传 image_idx + asset_ext，供后端 resolve 重新
        // 推导对象键并生成新的 asset_url（历史快照里 asset_url 已剥离且会过期）。
        image_idx: loc?.image_idx ?? undefined,
        asset_ext: loc?.asset_ext ?? undefined,
      });
    }
  }
  if (!parsed.length) return refs;
  // 端点是 `success_response(data=result)` 且 result 就是 list —— data 是裸数组。
  // 兼容 `{references}` 形状只是防御，不要照它写（旧代码就是这么整块丢掉的）。
  const result = await getData<KnowledgeReference[] | { references?: KnowledgeReference[] }>(
    request.post('/knowledge-base/documents/references/resolve', { refs: parsed }),
  );
  const resolved = Array.isArray(result) ? result : result?.references ?? [];
  if (!resolved.length) return refs;
  return mergeResolvedUrls(refs, resolved);
}

/**
 * 把 resolve 的新 URL 填回快照引用。恢复不到的（doc 被删/越权剔除）原样透传。
 *
 * 图片资产按 `image_idx` 对齐而非下标：resolve 侧 location 经「展开回传 → 按 doc 聚合」
 * 往返，顺序与条数都不保证，只有 image_idx 是稳定标识。
 */
function mergeResolvedUrls(
  snapshot: KnowledgeReference[],
  resolved: KnowledgeReference[],
): KnowledgeReference[] {
  const byDoc = new Map(resolved.map((r) => [r.doc_id, r]));
  return snapshot.map((ref) => {
    const hit = byDoc.get(ref.doc_id);
    if (!hit) return ref;
    const assetByIdx = new Map(
      (hit.locations ?? [])
        .filter((l) => l.image_idx != null && l.asset_url)
        .map((l) => [l.image_idx, l.asset_url] as const),
    );
    return {
      ...ref,
      raw_url: hit.raw_url ?? ref.raw_url,
      locations: (ref.locations ?? []).map((loc) => {
        const assetUrl = loc.image_idx != null ? assetByIdx.get(loc.image_idx) : undefined;
        return assetUrl ? { ...loc, asset_url: assetUrl } : loc;
      }),
    };
  });
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

/**
 * [jonex] 以图问知识：把图片识别成一段可直接检索的文本。
 *
 * 两步式的第一步 —— **它不检索**。拿到 `query` 后照常调 `streamKnowledgeSearch`，
 * 走现成的快速 / 深度 / Wiki 三条链路（那三条一行未改）。
 *
 * ⚠️ **不要手动设 `Content-Type`**：multipart 的 boundary 必须由浏览器生成，
 * 手写会把 boundary 写死，后端解析不出分片。
 * ⚠️ 图片字节在这里、也只在这里上传 —— 后端在「保存历史」那一刻才把它落到对象存储
 * （识别阶段只进 Redis 暂存），所以「识别了但没检索」的图不会留痕。
 */
export async function recognizeImageQuery(
  files: Blob[],
  question?: string,
  signal?: AbortSignal,
): Promise<ImageQueryResult> {
  const form = new FormData();
  files.forEach((file, index) => {
    form.append('files', file, `image_${index + 1}`);
  });
  if (question?.trim()) form.append('question', question.trim());

  const raw = await getData<RawImageQueryResponse>(
    request.post('/knowledge-base/search/image-query', form, {
      signal,
      // 识别比普通请求慢（VLM 往返），但仍是交互场景：实际超时由后端与 dev-gateway 决定
      timeout: 90_000,
    }),
  );
  return {
    query: raw.query,
    images: (raw.images ?? []).map((image) => ({
      index: image.index,
      status: image.status === 'ok' ? 'ok' : 'failed',
      errorCode: image.error_code ?? undefined,
      imageId: image.image_id ?? null,
      description: image.description ?? '',
      ocrText: image.ocr_text ?? '',
      entities: image.entities ?? [],
      cached: Boolean(image.cached),
      persisted: Boolean(image.persisted),
    })),
    entities: raw.entities ?? [],
    model: raw.model ?? '',
    partial: Boolean(raw.partial),
    durationMs: raw.duration_ms ?? 0,
    limits: raw.limits
      ? {
          maxImages: raw.limits.max_images ?? 4,
          maxBytes: raw.limits.max_bytes ?? 5 * 1024 * 1024,
          maxTotalBytes: raw.limits.max_total_bytes ?? 12 * 1024 * 1024,
        }
      : undefined,
  };
}

/**
 * [jonex] 以图问知识：取一张查询图片的短时查看地址。
 *
 * 用于历史条目的图片 `onError` 重试 —— 后端补签的 `viewUrl` 会过期（默认 15 分钟），
 * 而历史列表是快照，长时间停留在页面上就会失效。
 */
export async function getQueryImageViewTicket(
  imageId: string,
): Promise<{ url: string; expiresIn: number }> {
  const result = await getData<{ url: string; expires_in?: number; expiresIn?: number }>(
    request.get(`/knowledge-base/search/query-images/${imageId}/view-ticket`),
  );
  return { url: result.url, expiresIn: result.expires_in ?? result.expiresIn ?? 900 };
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
  // 快速检索默认走混合管线统一入口 /search/mix（后端按 kb_type 分流 lightrag / openkb /
  // jonex_wiki）；深度检索仍走 /search/deep（暂不支持 openkb，见 docs/openkb/llmwiki-reasoning）。
  //
  // [jonex] 但当选择范围明确落在**同一条引擎链路**上时改打该引擎的专用端点，这样前端拿到的
  // 就是该链路自己的检索过程与来源引用（/search/jonex_wiki → 推理链 stage=jonex_wiki_query）。
  // 判据是显式三分支逐类型判断（不用「非 jonex 即 openkb」这类否定式判断），且要求每个
  // kb_id 的类型都已知（kbTypes 与 kbIds 等长），否则无法断言「全库同引擎」，回落 /search/mix。
  const kbTypes = normalized.kbTypes;
  const typesKnown = kbTypes.length > 0 && kbTypes.length === normalized.kbIds.length;
  const engineType = typesKnown && new Set(kbTypes).size === 1 ? kbTypes[0] : '';
  const deep = params.deep === true;
  // [jonex] Wiki 检索（按钮组第三档）：显式走 jonex_wiki 专用端点，不看 kbTypes 的推断结果
  const wiki = params.wiki === true && !deep;
  // 专用 wiki 引擎端点：接收 query/mode/top_k/knowledge_base_ids，不接受严格模式参数
  const wikiEndpoint = wiki || (!deep && !!engineType);
  const endpoint = wiki
    ? '/knowledge-base/search/jonex_wiki'
    : deep
      ? '/knowledge-base/search/deep'
      : engineType === 'jonex_wiki'
        ? '/knowledge-base/search/jonex_wiki'
        : engineType === 'openkb'
          ? '/knowledge-base/search/llmwiki'
          : '/knowledge-base/search/mix';
  const wikiStream = endpoint === '/knowledge-base/search/jonex_wiki';
  const url = new URL(`${baseUrl}${endpoint}${wikiStream ? '/stream' : ''}`, window.location.origin);

  const strict = normalized.strictConfig;
  const body: Record<string, unknown> = {
    query: normalized.query,
    mode: normalized.mode,
    top_k: normalized.topK,
    with_reasoning: true,
  };
  if (endpoint === '/knowledge-base/search/jonex_wiki') {
    body.thinking_mode = params.wikiThinkingMode ?? 'fast';
  }
  if (strict && !wikiEndpoint) {
    // 严格模式参数（普通 / 深度检索共用）：strict_mode 关闭时仅传开关，不传其它严格参数
    body.strict_mode = strict.strict_mode;
    if (strict.strict_mode) {
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
  }
  // [jonex] 检索维度改为领域空间后，不再下发 domain_id（后端检索只认 knowledge_base_ids）
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
    const failure = await response.json().catch(() => null);
    throw new Error(failure?.message || failure?.detail || `Search failed: ${response.status}`);
  }

  try {
    if (wikiStream) {
      let sessionId = '';
      let completed = false;
      let cancellation: Promise<unknown> | undefined;
      const stop = () => {
        if (!sessionId) return;
        cancellation ??= fetch(new URL(`${baseUrl}${endpoint}/cancel`, window.location.origin).toString(), {
          method: 'POST', keepalive: true,
          headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId }),
        }).catch(() => undefined);
        return cancellation;
      };
      signal?.addEventListener('abort', stop, { once: true });
      try {
        await consumeWikiSearchStream(response, { ...handlers,
          onDone: (meta) => { completed = true; handlers.onDone?.(meta); },
        }, (id) => { sessionId = id; if (signal?.aborted) void stop(); });
      } finally {
        signal?.removeEventListener('abort', stop);
        if (!completed) await stop();
      }
      return;
    }
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
      // [jonex] answer-feedback 锚点：后端检索响应下发检索历史记录 id
      history_id: result.data.history_id,
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

// ── answer-feedback（回答反馈，锚点 history_id）API ────────────

/** 提交/更新回答反馈（operation_id 幂等，version 单调递增，切换类型直接覆盖） */
export async function submitAnswerFeedback(params: SubmitAnswerFeedbackParams): Promise<SubmitAnswerFeedbackResponse> {
  const token = readAccessToken();
  const response = await fetch('/api/v1/knowledge-base/search/answer-feedback', {
    method: 'POST',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      history_id: params.historyId,
      operation_id: params.operationId,
      version: params.version,
      feedback_type: params.feedbackType,
      feedback_reason: params.feedbackReason ?? undefined,
      feedback_comment: params.feedbackComment?.trim() ? params.feedbackComment.trim() : undefined,
    }),
  });
  if (!response.ok) {
    throw new Error('Failed to submit feedback');
  }
  const result = await response.json();
  return (
    result.data ?? {
      feedback: {
        id: '',
        history_id: params.historyId,
        version: params.version,
        feedback_type: params.feedbackType,
        feedback_reason: params.feedbackReason ?? null,
        feedback_comment: params.feedbackComment ?? null,
      },
      superseded: false,
      idempotent: false,
    }
  );
}

/** 回显某条检索历史（history_id）的回答反馈，无反馈时 feedback 为 null */
export async function getAnswerFeedback(historyId: string): Promise<AnswerFeedbackEchoResponse> {
  const token = readAccessToken();
  const searchParams = new URLSearchParams({ history_id: historyId });
  const response = await fetch(`/api/v1/knowledge-base/search/answer-feedback?${searchParams}`, {
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!response.ok) throw new Error('Failed to fetch feedback');
  const result = await response.json();
  return result.data ?? { feedback: null };
}

/** 按知识库聚合查询回答反馈列表（情况追踪页使用） */
export async function getAnswerFeedbackList(
  knowledgeBaseId: string,
  params?: { feedbackType?: SearchFeedbackType; page?: number; pageSize?: number },
): Promise<AnswerFeedbackListResponse> {
  const token = readAccessToken();
  const searchParams = new URLSearchParams({ knowledge_base_id: knowledgeBaseId });
  if (params?.feedbackType) searchParams.set('feedback_type', params.feedbackType);
  if (params?.page) searchParams.set('page', String(params.page));
  if (params?.pageSize) searchParams.set('page_size', String(params.pageSize));

  const response = await fetch(`/api/v1/knowledge-base/search/answer-feedback/list?${searchParams}`, {
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!response.ok) throw new Error('Failed to fetch answer feedback list');
  const result = await response.json();
  return result.data ?? { items: [], total: 0, like_count: 0, dislike_count: 0, page: 1, page_size: 50 };
}

/** 切换回答反馈采纳状态 */
export async function toggleAnswerFeedbackAdopted(feedbackId: string): Promise<void> {
  const token = readAccessToken();
  const response = await fetch('/api/v1/knowledge-base/search/answer-feedback/toggle-adopt', {
    method: 'POST',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ feedback_id: feedbackId }),
  });
  if (!response.ok) throw new Error('Failed to toggle answer feedback adopted');
}

/** 获取知识库的回答反馈统计 */
export async function getAnswerFeedbackStats(knowledgeBaseId: string): Promise<AnswerFeedbackStats> {
  const token = readAccessToken();
  const response = await fetch(
    `/api/v1/knowledge-base/search/answer-feedback/stats?knowledge_base_id=${knowledgeBaseId}`,
    { headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) } },
  );
  if (!response.ok) throw new Error('Failed to fetch answer feedback statistics');
  const result = await response.json();
  return result.data ?? { total: 0, like_count: 0, dislike_count: 0 };
}

/** 删除回答反馈记录（软删除） */
export async function deleteAnswerFeedback(feedbackId: string): Promise<void> {
  const token = readAccessToken();
  const response = await fetch(`/api/v1/knowledge-base/search/answer-feedback/${feedbackId}`, {
    method: 'DELETE',
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!response.ok) throw new Error('Failed to delete answer feedback');
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

export interface WikiEvidenceView {
  kind: string;
  exact_location: boolean;
  query_id: string;
  bundle_sha: string;
  segments: { text: string; highlight: boolean }[];
  spans?: number[][];
  page_number?: number | null;
  sheet_name?: string | null;
  table_title?: string;
  rows?: number[];
  columns?: number[];
  cells?: { row: number; column: number; coordinate: string; value: string | null; formula?: string | null; highlight: boolean }[];
  truncated?: boolean;
}

export async function resolveWikiEvidence(ref: KnowledgeReference, signal?: AbortSignal): Promise<WikiEvidenceView> {
  return getData<WikiEvidenceView>(request.post('/knowledge-base/search/jonex_wiki/evidence', {
    knowledge_base_id: ref.kb_id, query_id: ref.query_id, handle: ref.evidence_handle,
  }, { signal }));
}
