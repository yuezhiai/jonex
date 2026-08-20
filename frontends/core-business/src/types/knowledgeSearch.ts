export type KnowledgeSearchMode = 'local' | 'global' | 'hybrid' | 'naive' | 'mix' | 'bypass';

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
  description?: string;
  domain_type?: string;
  status?: string;
  space_id?: string;
  space_name?: string;
  kb_ids?: string[];
  kb_names?: string[];
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

export interface KnowledgeSearchStreamParams {
  query: string;
  mode?: KnowledgeSearchMode;
  topK?: number;
  domainId?: string;
  /** 知识库 ID 列表，至少一个 */
  kbIds?: string[];
  /** 是否深度检索：true 时调用 /search/deep，默认 /search/ontology */
  deep?: boolean;
  /** 严格模式配置（缺省时后端使用默认值） */
  strictConfig?: KnowledgeSearchStrictConfig;
}

/** 引用位置（与后端 SourceLocation 对齐） */
export interface KnowledgeReferenceLocation {
  type: 'chunk' | 'char' | 'page' | 'timestamp' | 'document' | 'image';
  chunk_index?: number | null;
  char_start?: number | null;
  char_end?: number | null;
  page_no?: number | null;
  time_start?: number | null;
  time_end?: number | null;
  /** [jonex] §image-refs：图片位置——文档内嵌图片的模态序号（全局枚举） */
  image_idx?: number | null;
  /** [jonex] §image-refs：图片资产预览地址（预签名 URL；local 后端/未上传为空，前端降级） */
  asset_url?: string | null;
  /** 命中片段原文文本（RAG 链路带 chunk content 时有值） */
  text?: string | null;
}

/** 结构化引用（与后端 SourceReference 对齐） */
export interface KnowledgeReference {
  doc_id: string;
  kb_id?: string | null;
  file_name: string;
  mime_type?: string | null;
  file_size?: number | null;
  media_type: 'text' | 'pdf' | 'audio' | 'video' | 'image' | 'other';
  raw_url?: string | null;
  wiki_path?: string | null;  // [jonex] OpenKB wiki 页路径
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
}

export interface KnowledgeSearchStreamHandlers {
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

export type KnowledgeSearchRunStatus = 'idle' | 'searching' | 'done' | 'empty' | 'stopped' | 'error';

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
