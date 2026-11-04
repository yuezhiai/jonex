import type { KnowledgeBaseType } from './domainKnowledge';
import type { ResultBlock } from './resultBlocks';

export type KnowledgeSearchMode = 'local' | 'global' | 'hybrid' | 'naive' | 'mix' | 'bypass';

/**
 * [jonex] 检索模式（搜索框下方档位下拉）：
 * - fast：快速检索 → /search/mix（按 kb_type 分流 lightrag / openkb / jonex_wiki）
 * - deep：深度检索 → /search/deep
 * - wiki：Wiki 检索 → 强制走 jonex_wiki 专用端点 /search/jonex_wiki，
 *   只检索 jonex_wiki 链路的库，展示 Wiki 检索链路（stage=jonex_wiki_query）与页面级来源引用
 * - ontologyData：本体 + 数据 → /search/ontology-data（本体检索后追加数据库查询结果）
 * - data：数据检索 → /search/data（纯 NL→SQL，不走本体）
 */
export type KnowledgeSearchUiMode = 'fast' | 'deep' | 'wiki' | 'ontologyData' | 'data';

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
  queryImageIds?: string[];
  imageQuestion?: string;
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
  /** 显式「本体 + 数据」路线：true 时调用 /search/ontology-data（本体检索后追加数据库查询结果） */
  ontologyData?: boolean;
  /** 显式纯「数据检索」路线：true 时调用 /search/data（直接 NL→SQL，不走本体） */
  data?: boolean;
  /**
   * [jonex] 是否 Wiki 检索：true 时强制调用 /search/jonex_wiki（jonex_wiki 专用链路）。
   * 该端点拒绝异管线知识库，调用方需先把 kbIds 收敛为 kb_type=jonex_wiki 的库。
   */
  wiki?: boolean;
  wikiThinkingMode?: WikiThinkingMode;
  /** 严格模式配置（缺省时后端使用默认值） */
  strictConfig?: KnowledgeSearchStrictConfig;
  /**
   * [①②] 点名 pin `{data_source_id: entity_name}`：点 ② 模糊块的候选 chip 重问时带上。
   * 路由层用来钉死该源 ② 调用的 `entity_name`；**转发 Jonex 前会被剥掉**（检索本身不受影响）。
   */
  pins?: Record<string, string>;
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
  asset_id?: string | null;
  bundle_sha?: string | null;
  work_title?: string | null;
  author?: string | null;
  source_locator?: Record<string, unknown> | null;
  usage?: 'illustration' | 'visual_evidence' | null;
  association_method?: string | null;
  visual_read_id?: string | null;
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

/** 推理步骤单个输入/输出展示项（与后端 ReasoningIOItem 对齐，已脱敏、已裁剪） */
export interface ReasoningIOColumn {
  key: string;
  label: string;
}

export interface ReasoningIOItem {
  label: string;
  type: 'text' | 'list' | 'kv' | 'table' | 'json';
  value?: unknown;
  /** table：列定义 */
  columns?: ReasoningIOColumn[];
  /** table：行数据（服务端最多 50 行） */
  rows?: Record<string, unknown>[];
  /** table：真实总条数（可能大于 rows.length） */
  total?: number | null;
  /** table：默认展示行数 */
  preview_rows?: number | null;
  /** table：rows 是否已被服务端截断 */
  truncated?: boolean;
  note?: string | null;
}

/** 推理链单步（与后端 ReasoningStep 对齐） */
export interface ReasoningStep {
  stage: string;
  title: string;
  status: 'running' | 'done' | 'skipped' | 'failed' | 'degraded';
  summary?: string | null;
  detail?: Record<string, unknown> | null;
  duration_ms?: number | null;
  /** 步骤输入参数（展示型，已脱敏；旧历史快照可能缺失） */
  input?: ReasoningIOItem[] | null;
  /** 步骤输出结果（展示型，已脱敏；旧历史快照可能缺失） */
  output?: ReasoningIOItem[] | null;
}

/** 推理链（与后端 ReasoningTrace 对齐） */
export interface ReasoningTrace {
  steps: ReasoningStep[];
  final_source: string;
  /** 实际检索策略（mixed / ontology / rag / llm-wiki / jonex-wiki / none）；旧快照缺失 */
  strategy?: string | null;
  total_ms?: number | null;
  /** 整份 trace 因体积被服务端裁剪过 */
  io_truncated?: boolean;
  /** {kb_id: 名称}；旧快照缺失时前端回落显示 kb_id */
  kb_names?: Record<string, string>;
}

/** [①②] ②（NL→SQL）`ambiguous` 时回的候选（抽象层概念候选的归一化形状）。 */
export interface DataBlockCandidate {
  entity_name: string;
  /** 中文展示名（抽象的 display_name；空/与 entity_name 相同 ⇒ 仍用原文，附在括号里）。 */
  display_name?: string | null;
  /** 指标候选落点实体的中文名（如「集团吨发日汇总」；概念候选为空）。 */
  entity_display_name?: string | null;
  /** 候选来源：concept = 概念候选，metric = 指标候选。 */
  kind?: 'concept' | 'metric' | string | null;
  /** 指标名（仅 kind=metric）。 */
  metric?: string | null;
  /** 命中方式：exact/alias/attribute/core/description/aggregate（聚合词兜底）。 */
  match_kind?: string | null;
  concept?: string | null;
  matched_term?: string | null;
  data_source_id?: string | null;
  /** 数据源名（wte_a 生产库等；候选 chip 用它区分 A/B 两个同名实体）。 */
  source_name?: string | null;
}

/** [①②] 数据路径的**结构化按源块**（router 的 `_merge` 下发）。
 *
 * DI 块本期只有 `text`；NL→SQL（②）块带 `table`/`sql`，前端渲染真表格 + 引擎标。
 * 老的/开关关的后端**不带这个字段** ⇒ 前端渲染路径逐字不变。 */
export interface DataBlock {
  data_source_id: string;
  source_name: string;
  engine: 'di' | 'nl2sql';
  kind: 'answer' | 'clarify' | 'error' | 'not_wired';
  text: string;
  table?: { columns: string[]; rows: unknown[][] } | null;
  sql?: string | null;
  row_count?: number | null;
  truncated?: boolean | null;
  reason?: string | null;
  /** ② ambiguous 的候选（点名用）；其余场合为 null。 */
  candidates?: DataBlockCandidate[] | null;
  /** W4.6 结果契约；老后端/无契约 → null 或键缺席，前端走旧渲染。 */
  blocks?: ResultBlock[] | null;
}

/** [①②] `/search/data` 路线的数据引用（SQL/表格单独展示，不混进正文）。 */
export interface DataReference {
  data_source_id?: string | null;
  source_name: string;
  sql?: string | null;
  table?: { columns: string[]; rows: unknown[][] } | null;
  row_count?: number | null;
  truncated?: boolean | null;
}

/** 血缘解释块的单条边端点（上游/下游各一条）。 */
export interface DataLineageEndpoint {
  endpoint: string;
  column?: string;
  origin?: string;
  confidence?: number;
}

/** 一个命中实体落到物理表后的「来源/下游」条目。 */
export interface DataLineageItem {
  entity_type: string;
  data_source_id: string;
  table: string;
  upstream: DataLineageEndpoint[];
  downstream: DataLineageEndpoint[];
  /** true = 血缘没读到（读不到 ≠ 没有）——UI 必须显式渲染，不能当空。 */
  unknown: boolean;
}

/** 本体问答的「数据来源/下游」解释块（后端 `data_lineage`；无内容时整个键缺席）。 */
export interface DataLineageBlock {
  items: DataLineageItem[];
  truncated?: boolean;
}

export interface KnowledgeSearchStreamMeta {
  source?: string;
  references?: KnowledgeReference[];
  reasoning?: ReasoningTrace | null;
  rag_used?: boolean;
  /** [jonex] answer-feedback 锚点：后端检索响应下发的检索历史记录 id（回放历史用 history item id） */
  history_id?: string;
  /** [①②] 数据路径结构化块（router 扇出路径的下发；不存在即无此数据路径）。 */
  data_blocks?: DataBlock[] | null;
  /** [①②] 文档答案 + 数据补充的并置标记；为 true 时保留打字、表格在下方渲染。 */
  data_supplement?: boolean;
  /** [①②] `/search/data` 路线的数据引用（SQL/表格单独展示）。 */
  data_references?: DataReference[] | null;
  /** [A] 血缘解释块（后端只在有内容时下发；不存在即无该块，渲染零回归）。 */
  data_lineage?: DataLineageBlock | null;
  /** 实时数据：本次问答调用动作绑定外部工具的摘要（后端只在调用过时下发）。 */
  tool_results?: ToolCallResult[] | null;
}

/** 问答调用外部工具的一条摘要（不含入参与输出原文；输出已作为答案依据）。 */
export interface ToolCallResult {
  seq: number;
  action: string;
  tool_name?: string | null;
  tool_label?: string | null;
  tool_source?: 'platform' | 'kb' | null;
  /** executed / failed / no_runtime / invalid_input / not_callable / needs_confirmation / not_allowed / rate_limited / tool_unavailable */
  status?: string | null;
  http_status?: number | null;
  called_at?: string | null;
  fact_no?: number | null;
}

export interface WikiProgressEvent {
  seq: number;
  kb_id?: string;
  type?: string;
  title?: string;
  tool?: string;
  /** 起始时刻（ISO 8601）：运行中的步骤用它算自己的耗时 */
  started_at?: string;
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
  /** table_locate 的字面检索词与目标字段 */
  literal?: string;
  field_id?: string;
  range?: [number, number];
  /** table_read 的行窗口（引擎从 scope.row_start/row_end 摊平） */
  rows?: [number, number];
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
  /** Web 反馈有值；MCP 反馈不保存问答快照，user_id / history_id / query 均为 null */
  user_id: string | null;
  history_id: string | null;
  query: string | null;
  answer: string | null;
  feedback_type: SearchFeedbackType;
  feedback_reason: FeedbackReason | null;
  feedback_comment: string | null;
  knowledge_base_ids: string[];
  source: string | null;
  mode: string | null;
  source_missing_reason: string | null;
  /** MCP 反馈归因字段；Web 反馈为 null。有 mcp_key_id 即视为 MCP 来源 */
  mcp_key_id: string | null;
  mcp_key_name: string | null;
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
