import type { KnowledgeBaseType } from './domainKnowledge';

export type KnowledgeSearchMode = 'local' | 'global' | 'hybrid' | 'naive' | 'mix' | 'bypass';

/**
 * [jonex] 检索模式（搜索框右侧按钮组的三个档位）：
 * - fast：快速检索 → /search/mix（按 kb_type 分流 lightrag / openkb / jonex_wiki）
 * - deep：深度检索 → /search/deep
 * - wiki：Wiki 检索 → 强制走 jonex_wiki 专用端点 /search/jonex_wiki，
 *   只检索 jonex_wiki 链路的库，展示 Wiki 检索链路（stage=jonex_wiki_query）与页面级来源引用
 */
export type KnowledgeSearchUiMode = 'fast' | 'deep' | 'wiki';

export interface KnowledgeSearchOverview {
  totalDocuments: number;
  totalEntities: number;
  totalRelations: number;
  todaySearches: number;
  avgResponseTimeMs: number;
  totalDomains?: number;
  sourceFiles?: number;
  dataSources?: number;
}

export interface KnowledgeSearchDomain {
  id: string;
  name: string;
  /** 检索范围维度：领域空间（space）或领域服务（service） */
  kind: 'space' | 'service';
  description?: string;
  domain_type?: string;
  status?: string;
  space_id?: string;
  space_name?: string;
  kb_ids?: string[];
  kb_names?: string[];
  /**
   * [jonex] kb_ids 各自的知识库类型（kb_type），与 kb_ids 顺序/下标一一对应。
   * 仅当每个 kb_id 的类型都已知时才等长；前端据此决定「只选了一条引擎链路」
   * 时打哪条专用检索端点（/search/jonex_wiki、/search/llmwiki），否则回落 /search/mix。
   */
  kb_types?: KnowledgeBaseType[];
  created_at?: string;
  updated_at?: string;
}

export interface KnowledgeSearchHistoryItem {
  id: string;
  query: string;
  searchedAt: string;
  resultCount: number;
  domain?: string;
  domainId?: string;
  domainSpaceId?: string;
  status?: 'done' | 'stopped' | 'error';
  answerPreview?: string;
  referenceCount?: number;
  durationMs?: number;
  mode?: 'hybrid';
  topK?: number;
  /** [jonex] 历史快照：完整答案原始文本（含 <think> 标记），点击历史直接展示 */
  answer?: string;
  /** [jonex] 历史快照：引用快照（raw_url 已剥离，展示时重新富化） */
  references?: KnowledgeReference[];
  /** [jonex] 历史快照：推理链（ReasoningTrace），点击历史直接展示推理过程 */
  reasoning?: ReasoningTrace | null;
  /** [jonex] 检索条件快照：严格模式配置（再次搜索时复用当时条件） */
  strictConfig?: KnowledgeSearchStrictConfig | null;
  /**
   * [jonex] 以图问知识：这条历史附带的查询图片（按用户附图顺序）。
   * 已清理的图不会出现在这里；无图时是空数组。
   * `viewUrl` 由后端补签（短时票据），local 后端下也可能为空。
   */
  queryImages?: QueryImageRef[];
  /** [jonex] 以图问知识：用户当时输入的原话（可为空；`query` 存的是识别产出的检索文本） */
  imageQuestion?: string;
}

export interface SaveKnowledgeSearchHistoryPayload {
  query: string;
  resultCount: number;
  domain?: string;
  domainId?: string;
  domainSpaceId?: string;
  status: 'done';
  answerPreview?: string;
  referenceCount?: number;
  durationMs?: number;
  mode?: 'hybrid';
  topK?: number;
  /** [jonex] 历史快照：完整答案 + 引用 + 推理链 */
  answer?: string;
  references?: KnowledgeReference[];
  reasoning?: ReasoningTrace | null;
  /** [jonex] 检索条件快照：严格模式配置（再次搜索时复用当时条件） */
  strictConfig?: KnowledgeSearchStrictConfig | null;
  /**
   * [jonex] 以图问知识：本次检索附带的查询图片 id（**顺序即展示顺序**）。
   * 后端在保存历史这一刻才把图片落盘（识别阶段只暂存），所以这里传的是识别接口
   * 回包里的 `imageId`；过期 / 没识别成功的图会被后端跳过，历史照常保存。
   */
  queryImageIds?: string[];
  /** [jonex] 以图问知识：用户原话（存进历史 metadata，与识别产出的 `query` 分开） */
  imageQuestion?: string;
}

// ── 以图问知识（图片 → 识别文本 → 复用现有检索链路）────────────────
// 设计：docs/knowledge-base/image-query-search-execution-plan.md §5-F1

/** 单张图片的识别结果（与后端 `ImageQueryImageOut` 对齐） */
export interface ImageQueryImageResult {
  /** 对应提交时的顺序 */
  index: number;
  status: 'ok' | 'failed';
  /** 失败时的平台错误码（用于挑文案） */
  errorCode?: number | null;
  /** 保存历史时回传；失败 / 暂存失败 / 开关关闭时为 null */
  imageId?: string | null;
  description: string;
  ocrText: string;
  entities: string[];
  /** 识别结果来自缓存或已持久化记录 */
  cached: boolean;
  /** imageId 是否已是持久化图片（同图复用） */
  persisted: boolean;
}

/** 识别接口回包（与后端 `ImageQueryResponse` 对齐） */
export interface ImageQueryResult {
  /** 合并后直接交给检索接口的文本 */
  query: string;
  images: ImageQueryImageResult[];
  entities: string[];
  model: string;
  /** 是否有图片识别失败 */
  partial: boolean;
  durationMs: number;
  /** 后端生效中的限制值，前端缓存后以此为准 */
  limits?: ImageQueryLimits;
}

export interface ImageQueryLimits {
  maxImages: number;
  maxBytes: number;
  maxTotalBytes: number;
}

/** 历史条目上的图片引用 */
export interface QueryImageRef {
  id: string;
  mimeType: string;
  fileSize: number;
  /** 短时查看地址（后端补签；可能为空） */
  viewUrl?: string | null;
}

/** 输入区待检索的一张图（前端本地态） */
export interface AttachedImage {
  /** 压缩后的字节（识别与落盘用的都是这份，原图不出浏览器） */
  file: Blob;
  /** 本地预览地址（objectURL，需在移除/卸载时 revoke） */
  objectUrl: string;
  name: string;
  /** 压缩后的字节数（用于前端预判上限，与后端判据同源） */
  size: number;
}

/** 严格模式配置项（随搜索请求下发，普通/深度共用前 5 项，深度额外含后 2 项） */
export interface KnowledgeSearchStrictConfig {
  strict_mode: boolean;
  strict_max_attempts: number;
  strict_min_score: number;
  strict_require_reference: boolean;
  strict_require_grounded: boolean;
  /** 深度检索专属 */
  max_subqueries: number;
  /** 深度检索专属 */
  allow_common_sense: boolean;
}

export type WikiThinkingMode = 'fast' | 'standard' | 'deep';

export interface KnowledgeSearchStreamParams {
  query: string;
  mode?: KnowledgeSearchMode;
  topK?: number;
  domainId?: string;
  /** 知识库 ID 列表，至少一个 */
  kbIds?: string[];
  /**
   * [jonex] 与 kbIds 对应的 kb_type 列表（用于端点选择）。
   * 只在「每个 kb_id 的类型都已知且完全一致」时才用于选择专用端点；否则一律走 /search/mix。
   */
  kbTypes?: KnowledgeBaseType[];
  /** 是否深度检索：true 时调用 /search/deep，默认 /search/ontology */
  deep?: boolean;
  /**
   * [jonex] 是否 Wiki 检索：true 时强制调用 /search/jonex_wiki（jonex_wiki 专用链路）。
   * 该端点拒绝异管线知识库，调用方需先把 kbIds 收敛为 kb_type=jonex_wiki 的库。
   */
  wiki?: boolean;
  wikiThinkingMode?: WikiThinkingMode;
  /** 严格模式配置（缺省时后端使用默认值） */
  strictConfig?: KnowledgeSearchStrictConfig;
}

/** 引用位置（与后端 SourceLocation 对齐） */
export interface KnowledgeReferenceLocation {
  type: 'chunk' | 'char' | 'page' | 'timestamp' | 'document' | 'image' | 'table_row';
  evidence_ref?: Record<string, unknown> | null;
  chunk_index?: number | null;
  char_start?: number | null;
  char_end?: number | null;
  page_no?: number | null;
  time_start?: number | null;
  time_end?: number | null;
  /** [jonex] §image-refs：图片位置——文档内嵌图片的模态序号（全局枚举） */
  image_idx?: number | null;
  /** [jonex] 表格行引用：表格在页面内的序号（0-based） */
  table_idx?: number | null;
  /** [jonex] 表格行引用：行起止（0-based，闭区间） */
  row_start?: number | null;
  row_end?: number | null;
  /** [jonex] 表格单元格列起止（0-based，闭区间） */
  cell_start?: number | null;
  cell_end?: number | null;
  /** [jonex] §image-refs：图片资产预览地址（预签名 URL；local 后端/未上传为空，前端降级） */
  asset_url?: string | null;
  /** [jonex] §image-refs B2：图片资产扩展名（持久标识；resolve 回传据此重新推导 asset_url） */
  asset_ext?: string | null;
  /** 命中片段原文文本（RAG 链路带 chunk content 时有值） */
  text?: string | null;
}

/** 结构化引用（与后端 SourceReference 对齐） */
export interface KnowledgeReference {
  query_id?: string | null;
  evidence_handle?: string | null;
  bundle_sha?: string | null;
  doc_id: string;
  kb_id?: string | null;
  kb_name?: string | null; // [jonex] 知识库名称（冗余，方便前端展示，无需二次查询）
  file_name: string;
  mime_type?: string | null;
  file_size?: number | null;
  media_type: 'text' | 'pdf' | 'audio' | 'video' | 'image' | 'office' | 'other';
  raw_url?: string | null;
  wiki_path?: string | null; // [jonex] OpenKB wiki 页路径
  locations: KnowledgeReferenceLocation[];
}

/** 推理链单步（与后端 ReasoningStep 对齐） */
export interface ReasoningStep {
  stage: string;
  title: string;
  status: 'running' | 'done' | 'skipped' | 'failed';
  summary?: string | null;
  detail?: Record<string, unknown> | null;
  duration_ms?: number | null;
}

/** 推理链（与后端 ReasoningTrace 对齐） */
export interface ReasoningTrace {
  steps: ReasoningStep[];
  final_source: string;
  total_ms?: number | null;
}

export interface KnowledgeSearchStreamMeta {
  source?: string;
  references?: KnowledgeReference[];
  reasoning?: ReasoningTrace | null;
  rag_used?: boolean;
  /** [jonex] answer-feedback 锚点：后端检索响应下发的检索历史记录 id（回放历史用 history item id） */
  history_id?: string;
}

export interface WikiProgressEvent {
  seq: number;
  kb_id?: string;
  type?: string;
  title?: string;
  tool?: string;
  duration_ms?: number;
  input_summary?: unknown;
  result_summary?: unknown;
  details?: WikiProgressDetails;
}

export interface WikiProgressDetails {
  query?: string;
  paths?: string[];
  table_id?: string;
  operation?: string;
  range?: [number, number];
  previews?: { title?: string; path?: string; excerpt?: string }[];
}

export interface KnowledgeSearchStreamHandlers {
  onProgress?: (events: WikiProgressEvent[]) => void;
  onDelta: (content: string, meta?: KnowledgeSearchStreamMeta) => void;
  onDone?: (meta?: KnowledgeSearchStreamMeta) => void;
  onError?: (error: Error) => void;
}

export type KnowledgeSearchViewStatus = 'initial' | 'loading' | 'searching' | 'done' | 'empty' | 'error';

// ── 情况追踪（搜索反馈管理） ──────────────────────────────────

/** 搜索反馈单条记录 */
export interface SearchFeedbackItem {
  id: string;
  tenant_id: string;
  user_id: string;
  session_id: string;
  query: string;
  answer_preview: string | null;
  knowledge_base_id: string;
  knowledge_base_name: string | null;
  feedback_type: SearchFeedbackType;
  adopted: boolean;
  searched_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** 搜索反馈列表响应 */
export interface SearchFeedbackListResponse {
  items: SearchFeedbackItem[];
  total: number;
  like_count: number;
  dislike_count: number;
  page: number;
  page_size: number;
}

/** 搜索反馈统计 */
export interface SearchFeedbackStats {
  total: number;
  like_count: number;
  dislike_count: number;
}

export type KnowledgeSearchRunStatus =
  | 'idle'
  | 'searching'
  | 'done'
  | 'empty'
  | 'stopped'
  | 'error'
  /** [jonex] 以图问知识：正在识别图片（检索还没开始）。卡片上分两段显示
   *  「识别图片中… → 检索中…」，用户才知道这会儿在等什么。 */
  | 'recognizing';

/** 搜索结果反馈类型 */
export type SearchFeedbackType = 'like' | 'dislike';

/** 提交结果反馈的参数（按搜索会话评价回答质量） */
export interface SubmitSearchFeedbackParams {
  sessionId: string;
  query: string;
  answerPreview: string;
  feedbackType: SearchFeedbackType;
  /** 搜索结果引用的知识库 ID 列表，用于按 KB 分别存储 */
  kbIds: string[];
  /** 搜索时间（ISO 格式） */
  searchedAt?: string;
}

/** 提交反馈后的响应 */
export interface SubmitSearchFeedbackResponse {
  success: boolean;
  feedbackType: SearchFeedbackType;
  likeCount: number;
  dislikeCount: number;
}

/** 取消结果反馈的参数 */
export interface CancelSearchFeedbackParams {
  sessionId: string;
  feedbackType: SearchFeedbackType;
  kbIds: string[];
}

// ── answer-feedback（回答反馈，锚点 history_id，operation_id 幂等 + version 版本化） ──────────

/** 点踩原因枚举（dislike 必填其一） */
export type FeedbackReason =
  'inaccurate' | 'not_answered' | 'incomplete' | 'wrong_reference' | 'missing_knowledge' | 'other';

/** 回答反馈主记录（回显 / 提交响应共用） */
export interface AnswerFeedbackRecord {
  id: string;
  history_id: string;
  operation_id?: string | null;
  /** 版本号：每个 history_id 从 1 开始递增，服务端丢弃旧 version（superseded） */
  version: number;
  feedback_type: SearchFeedbackType;
  feedback_reason?: FeedbackReason | null;
  feedback_comment?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** 提交回答反馈的参数 */
export interface SubmitAnswerFeedbackParams {
  /** 检索历史记录 id（检索响应返回 / 历史项 id） */
  historyId: string;
  /** 操作幂等 id：新操作生成 uuid，重试复用 */
  operationId: string;
  /** 版本号：每个 history_id 从 1 开始，成功 +1 */
  version: number;
  feedbackType: SearchFeedbackType;
  /** dislike 必填 */
  feedbackReason?: FeedbackReason | null;
  /** 补充说明，≤300 字 */
  feedbackComment?: string;
}

/** 提交回答反馈后的响应 */
export interface SubmitAnswerFeedbackResponse {
  feedback: AnswerFeedbackRecord;
  /** 是否覆盖了旧 version 的反馈 */
  superseded: boolean;
  /** 是否幂等命中（同一 operation_id 重试） */
  idempotent: boolean;
}

/** 回答反馈回显响应（GET ?history_id=，无反馈时 feedback 为 null） */
export interface AnswerFeedbackEchoResponse {
  feedback: AnswerFeedbackRecord | null;
}

/** 回答反馈列表项（按知识库聚合查询，情况追踪页使用） */
export interface AnswerFeedbackListItem {
  id: string;
  tenant_id: string;
  user_id: string;
  history_id: string;
  query: string;
  answer: string | null;
  feedback_type: SearchFeedbackType;
  feedback_reason: FeedbackReason | null;
  feedback_comment: string | null;
  knowledge_base_ids: string[];
  source: string | null;
  mode: string | null;
  source_missing_reason: string | null;
  adopted: boolean;
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

/** 回答反馈列表响应 */
export interface AnswerFeedbackListResponse {
  items: AnswerFeedbackListItem[];
  total: number;
  like_count: number;
  dislike_count: number;
  page: number;
  page_size: number;
}

/** 回答反馈统计 */
export interface AnswerFeedbackStats {
  total: number;
  like_count: number;
  dislike_count: number;
}
