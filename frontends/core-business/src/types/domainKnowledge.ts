export type DomainKnowledgeStatus = 'synced' | 'syncing' | 'failed' | 'disabled';

export type DomainKnowledgeSourceType = 'api' | 'api_push' | 'storage' | 'file';

export interface DomainKnowledgeSpace {
  id: string;
  name: string;
}

export interface DomainKnowledgeItem {
  id: string;
  name: string;
  spaceId: string;
  spaceName: string;
  dataSourceTypes: DomainKnowledgeSourceType[];
  documentCount: number;
  status: DomainKnowledgeStatus;
  updatedAt: string;
  ownerName?: string;
  description?: string;
  /**
   * [jonex] 知识库类型。创建时选定后不可变。
   * 列表标签与编辑弹窗的只读展示依赖它；后端缺省/未知时按 lightrag 处理。
   */
  kbType?: KnowledgeBaseType;
}

export interface DomainKnowledgeListParams {
  keyword?: string;
  spaceId?: string;
  status?: DomainKnowledgeStatus;
  sourceType?: DomainKnowledgeSourceType;
  page: number;
  pageSize: number;
  sortField?: 'updatedAt' | 'documentCount' | 'name';
  sortOrder?: 'ascend' | 'descend';
}

export interface PaginationResult<T> {
  list: T[];
  pagination: {
    page: number;
    pageSize: number;
    total: number;
  };
}

export type DomainKnowledgePermissionRole = 'view' | 'manage';

export interface DomainKnowledgePermissionMember {
  userId: string;
  name: string;
  dept: string;
  avatarText: string;
  avatarColor: string;
  role: DomainKnowledgePermissionRole;
}

export interface DomainKnowledgePermissionPayload {
  members: Array<{
    userId: string;
    role: DomainKnowledgePermissionRole;
  }>;
}

export function getStatusTextMap(t: (key: string) => string): Record<DomainKnowledgeStatus, string> {
  return {
    synced: t('domainKnowledge.kbStatus.synced'),
    syncing: t('domainKnowledge.kbStatus.syncing'),
    failed: t('domainKnowledge.kbStatus.failed'),
    disabled: t('domainKnowledge.kbStatus.disabled'),
  };
}

export const statusColorMap: Record<DomainKnowledgeStatus, string> = {
  synced: 'success',
  syncing: 'processing',
  failed: 'error',
  disabled: 'default',
};

/** 知识库类型。lightrag=标准检索型（向量+图谱）；openkb=Wiki 编译型（LLM 编译） */
export type KnowledgeBaseType = 'lightrag' | 'openkb';

// ─── Detail Page Types ──────────────────────────────────

export interface DomainKnowledgeDetail {
  id: string;
  name: string;
  spaceId: string;
  spaceName: string;
  documentCount: number;
  entityCount: number;
  relationCount: number;
  compileVersionCount: number;
  status: DomainKnowledgeStatus;
  updatedAt: string;
  /** Neo4j 不可用时为 true，实体/关系数被降级为 0（非真实为 0） */
  ontologyDegraded?: boolean;
  /**
   * 处理管线。openkb 的知识库不跑本体抽取，「编译结果」Tab 需换成只读 Wiki 视图。
   * 后端缺省/未知时按 lightrag 处理。
   */
  kbType?: KnowledgeBaseType;
}

export interface DataSourceConfig {
  id: string;
  name: string;
  type: string;
  accessType: string;
  configJson: Record<string, any>;
  docs: number;
  status: string;
  desc: string;
  iconType: 'api' | 'upload' | 'storage';
  iconBg: string;
  iconColor: string;
  path: string;
  knowledgeBaseId: string;
}

export interface ParserFileConfig {
  type: string;
  iconType: 'pdf' | 'word' | 'excel' | 'ppt' | 'image' | 'audio' | 'video';
  iconColor: string;
  parser: string;
  status: string;
  knowledgeBaseId: string;
}

export interface OntologyTemplate {
  id: string;
  type: string;
  attrs: string;
  relations: string;
  version: string;
  status: string;
  knowledgeBaseId: string;
}

export type ValidationSeverity = '高' | '中' | '低';

export interface ValidationRule {
  id: string;
  name: string;
  entity: string;
  condition: string;
  severity: ValidationSeverity;
  status: string;
  knowledgeBaseId: string;
}

export const severityColorMap: Record<ValidationSeverity, string> = {
  高: 'error',
  中: 'warning',
  低: 'processing',
};

/** severity → translation key 映射（供 t() 使用） */
export const severityLabelKey: Record<ValidationSeverity, string> = {
  高: 'validation.severity.high',
  中: 'validation.severity.medium',
  低: 'validation.severity.low',
};

export interface PromptTemplate {
  id: string;
  name: string;
  stage: string;
  model: string;
  author: string;
  date: string;
  status: string;
  knowledgeBaseId: string;
}

export interface CompileEntity {
  id: string;
  name: string;
  type: string;
  attrs: number;
  relations: number;
  createdAt: string;
  knowledgeBaseId: string;
}

export interface EntityListParams {
  keyword?: string;
  entityType?: string;
  page: number;
  pageSize: number;
  knowledgeBaseId: string;
}

export interface EntityDistribution {
  label: string;
  pct: number;
  count: number;
  color: string;
}

export interface RelationDistribution {
  label: string;
  pct: number;
  count: number;
  color: string;
}

export interface LogicRule {
  id: string;
  name: string;
  type: string;
  condition: string;
  conclusion: string;
  confidence: string;
  status: string;
  knowledgeBaseId: string;
}

export interface RuleTextSegment {
  text: string;
  bold?: boolean;
  color?: string;
}

export interface ActionRule {
  id: string;
  name: string;
  status: string;
  triggerIconType: string;
  triggerIconBg: string;
  triggerIconColor: string;
  triggerLabel: string;
  triggerText: RuleTextSegment[];
  actionIconType: string;
  actionIconBg: string;
  actionIconColor: string;
  actionLabel: string;
  actionText: RuleTextSegment[];
  knowledgeBaseId: string;
}

export interface DomainKnowledgeResultStats {
  entityCount: number;
  relationCount: number;
  compileVersionCount: number;
  sourceFileCount: number;
}

// ─── 领域知识结果 V2 — 真实后端 API 类型 ──────────────────

/** Kb 维度统计（ontology/statistics） */
export interface OntologyStatistics {
  knowledge_base_id: string;
  knowledge_base_name: string;
  /** 知识库最后更新时间（ISO 字符串，来自 PG knowledge_info.updated_at），无则为 null */
  last_update_time?: string | null;
  source_file_count: number;
  ontology_instance_count: number;
  ontology_relation_count: number;
  /** Neo4j 不可用时为 true，本体计数降级为 0（PG 侧统计仍有效） */
  ontology_degraded?: boolean;
}

/** 实体类型聚合（ontology/entity-types item） */
export interface OntologyInstanceSummary {
  name: string;
  type: string;
  display_name: string;
  description: string;
  status: string;
  build_status: string;
  instance_count: number;
  attributes: Array<{ name: string; display_name: string; type: string; description?: string }>;
}

/** 关系类型聚合（ontology/relation-types item） */
export interface RelationInstanceSummary {
  name: string;
  display_name: string;
  description: string;
  source: string;
  target: string;
  source_display_name: string;
  target_display_name: string;
  cardinality: string;
  status: string;
  build_status: string;
  instance_count: number;
}

/** 本体实例行（Neo4j 实体） */
export interface OntologyInstanceRow {
  name: string;
  type: string;
  aliases: string[];
  attributes: Record<string, unknown> | null;
  description: string;
  confidence: number | null;
  doc_ids: string[];
}

/** 本体实例列表查询参数 */
export interface OntologyInstanceListParams {
  kbId: string;
  entityType?: string;
  keyword?: string;
  page?: number;
  pageSize?: number;
  docId?: string;
}

/** 关系实例行（Neo4j ONT_REL 关系） */
export interface RelationInstanceRow {
  source: string;
  source_type: string;
  relation_type: string;
  target: string;
  target_type: string;
  attributes: Record<string, unknown> | null;
  confidence: number | null;
}

/** 关系实例列表查询参数 */
export interface RelationInstanceListParams {
  kbId: string;
  relationType?: string;
  sourceName?: string;
  targetName?: string;
  sourceType?: string;
  targetType?: string;
  keyword?: string;
  page: number;
  pageSize: number;
  docId?: string;
}

// ═══════════════════════════════════════════════════════════
// ─── 本体实例/关系 CRUD 类型 ───
// ═══════════════════════════════════════════════════════════

/** 本体实例更新字段：名称/别名/描述/属性 */
export interface OntologyInstanceUpdates {
  name?: string;
  aliases?: string[];
  description?: string;
  attributes?: Record<string, unknown>;
}

/** 更新本体实例请求体（PUT /ontology/instances/{entity_type}/{canonical_name}） */
export interface UpdateOntologyInstanceRequest {
  updates: OntologyInstanceUpdates;
}

/** 更新本体实例响应 */
export interface UpdateOntologyInstanceResponse {
  updated: boolean;
}

/** 删除本体实例响应 */
export interface DeleteOntologyInstanceResponse {
  deleted: boolean;
}

/** 创建本体实例请求体 */
export interface CreateOntologyInstanceRequest {
  knowledge_base_id: string;
  entity_type: string;
  name: string;
  aliases?: string[];
  description?: string;
  attributes?: Record<string, unknown>;
}

/** 创建本体实例响应（返回已创建的实例行） */
export type CreateOntologyInstanceResponse = OntologyInstanceRow;

// ─── 本体关系 CRUD ───

/** 本体关系更新字段：关系类型/属性 */
export interface OntologyRelationUpdates {
  relation_type?: string;
  attributes?: Record<string, unknown>;
}

/** 更新本体关系请求体 */
export interface UpdateOntologyRelationRequest {
  knowledge_base_id: string;
  source_entity_type: string;
  source_canonical_name: string;
  relation_type: string;
  target_entity_type: string;
  target_canonical_name: string;
  updates: OntologyRelationUpdates;
}

/** 更新本体关系响应 */
export interface UpdateOntologyRelationResponse {
  updated: boolean;
}

/** 删除本体关系请求体 */
export interface DeleteOntologyRelationRequest {
  knowledge_base_id: string;
  source_entity_type: string;
  source_canonical_name: string;
  relation_type: string;
  target_entity_type: string;
  target_canonical_name: string;
}

/** 删除本体关系响应 */
export interface DeleteOntologyRelationResponse {
  deleted: boolean;
}

/** 创建本体关系请求体 */
export interface CreateOntologyRelationRequest {
  knowledge_base_id: string;
  source_entity_type: string;
  source_canonical_name: string;
  relation_type: string;
  target_entity_type: string;
  target_canonical_name: string;
  attributes?: Record<string, unknown>;
}

/** 创建本体关系响应 */
export type CreateOntologyRelationResponse = RelationInstanceRow;

/** 图谱节点（来自 Neo4j OntologyEntity，id 为 type:name 复合键） */
export interface OntologyGraphNode {
  id: string;
  name: string;
  type: string;
  aliases: string[];
  attributes: Record<string, unknown> | null;
  description: string;
  confidence: number | null;
  doc_ids: string[];
}

/** 图谱边（来自 Neo4j ONT_REL，源/目标为 canonical_name，需配合 source_type/target_type 构造复合 ID） */
export interface OntologyGraphEdge {
  source: string;
  source_type: string;
  target: string;
  target_type: string;
  label: string;
  confidence: number | null;
}

/** 完整图谱数据 */
export interface OntologyGraphData {
  nodes: OntologyGraphNode[];
  edges: OntologyGraphEdge[];
  /** 全量节点总数（受类型筛选约束，无视 limit） */
  total_nodes: number;
  /** 全量关系总数（受类型筛选约束） */
  total_relations: number;
  /** 全量分类型计数 {entity_type: count}，用于侧栏筛选与总数提示 */
  type_counts: Record<string, number>;
  /** 本次实际返回的节点数 */
  returned_nodes: number;
  /** 本次实际返回的边数 */
  returned_edges: number;
  /** 是否因 limit 被截断 */
  truncated: boolean;
  /** 本次请求的节点上限 */
  limit: number;
  /** Neo4j 不可用时为 true（返回空图） */
  degraded?: boolean;
  /** 降级原因说明 */
  degraded_reason?: string;
}

/** 图谱请求参数 */
export interface OntologyGraphParams {
  limit?: number;
  /** 仅返回这些实体类型的节点 */
  entityTypes?: string[];
}

/** 邻域展开返回数据（仅 nodes + edges 增量） */
export interface OntologyNeighborData {
  nodes: OntologyGraphNode[];
  edges: OntologyGraphEdge[];
  /** Neo4j 不可用时为 true（返回空增量） */
  degraded?: boolean;
  /** 降级原因说明 */
  degraded_reason?: string;
}

/** 文件夹项 */
export interface FolderItem {
  id: string;
  name: string;
  knowledge_base_id: string;
  is_preset: boolean;
  sort_order: number;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** 文件夹列表响应 */
export interface FolderListResponse {
  items: FolderItem[];
  total: number;
}

export interface GraphSummary {
  entityTypeCount: number;
  relationTotalCount: number;
  relationTypeCount: number;
  avgDegree: number;
}

export interface DocumentChunk {
  /** LightRAG chunk 主键（chunk-<md5>），用于选中/列表 key */
  chunk_id: string
  /** chunk 完整文本内容（原 content_summary 已废弃，请用 content） */
  content: string
  /** chunk 内容长度（字符数），由 content.length 推导 */
  content_length: number
  /** chunk token 数 */
  tokens: number | null
  /** chunk 在原文档中的顺序索引 */
  chunk_order_index: number | null
  /** LightRAG 内部文档 id（doc-<md5>） */
  full_doc_id: string | null
  /** file_source 原始字符串（含 tstart=/tend= 时间锚点） */
  file_path: string
  /** 视频/音频时间轴起点（秒），来自 file_source tstart= 锚点 */
  time_start: number | null
  /** 视频/音频时间轴终点（秒），来自 file_source tend= 锚点 */
  time_end: number | null
  /** 文档页码 */
  page_no: number | null
  /** 字符位置（起） */
  char_start: number | null
  /** 字符位置（止） */
  char_end: number | null
  /** file_source 中的 chunk 索引 */
  chunk_index: number | null
  /** 创建时间（unix 秒） */
  create_time: number | null
  /** 更新时间（unix 秒） */
  update_time: number | null

  // ── 以下为兼容旧字段，新代码不再使用 ──
  /** @deprecated 请用 chunk_id */
  id?: string
  /** @deprecated 请用 content */
  content_summary?: string
}

// ─── Manual Datasource Types ────────────────────────────

export interface ManualDocItem {
  id: string;
  name: string;
  type: string;
  size: string;
  uploader: string;
  uploadTime: string;
  /** 兼容旧展示字符串；新展示改用 docStatus/ontologyStatus 派生 phase（见 utils/docPhase）。 */
  status: string;
  /** 后端原始 status：pending/parsing/ready/failed/deleting/deleted。 */
  docStatus?: string;
  /** 后端原始 ontology_status：pending/extracting/ready/failed。 */
  ontologyStatus?: string;
  /** 解析失败原因。 */
  errorMessage?: string;
  /** 编译失败原因。 */
  ontologyError?: string;
  /** [jonex] LLM-Wiki 编译状态：NULL/compiling/compiled/stale/failed（顶层字段 llm_wiki_compile_status）。 */
  llmWikiCompileStatus?: string;
  /** [jonex] LLM-Wiki 编译失败原因（顶层字段 llm_wiki_compile_error）。 */
  llmWikiCompileError?: string;
  knowledgeBaseId: string;
  dataSourceType?: string;
  tags?: string[];
  /** 后端原始 mime_type（如 application/pdf, video/mp4）。 */
  mimeType?: string;
  /** 派生的媒体类型：video | document。 */
  mediaType?: 'video' | 'document';
}

export interface ManualDocListParams {
  knowledgeBaseId: string;
  page: number;
  pageSize: number;
  keyword?: string;
  status?: string;
  ontology_status?: string;
  /** 线性状态多选筛选（DocPhase[]，作为后端 phase 多值参数）。 */
  phase?: string[];
  folder_id?: string;
}

/**
 * 文档处理状态统计（GET /knowledge-base/documents/stats）。
 * 互斥四桶：processing + completed + compileFailed + parseFailed = total。
 */
export interface DocumentStatsResult {
  total: number;
  /** 处理中：待解析/解析中/待编译/编译中。 */
  processing: number;
  /** 已完成：status=ready AND ontology=ready。 */
  completed: number;
  /** 编译失败：status=ready AND ontology=failed（仍可搜索）。 */
  compileFailed: number;
  /** 解析失败：status=failed（不可用）。 */
  parseFailed: number;
}

// ─── Backend parse-result types (snake_case) ──────────────

export interface BackendParseResultSummary {
  knowledge_base_id: string;
  tenant_id: string;
  source: string;
  scope_mode: string;
  scope_warning?: string;
  status: string;
  documents_count: number;
  processed_documents_count: number;
  failed_documents_count: number;
  chunks_count: number;
  entities_count: number;
  relationships_count: number;
  compile_versions_count: number;
  last_updated_at: string | null;
  storage_files: Record<string, boolean>;
}

export interface BackendEntityItem {
  id: string;
  name: string;
  type: string;
  description: string;
  source_id: string;
  file_path: string;
  created_at: number | null;
  relations_count: number;
}

export interface BackendGraphSummary {
  nodes_count: number;
  edges_count: number;
  entity_type_count: number;
  relation_type_count: number;
  avg_degree: number;
  entity_type_distribution: { label: string; count: number; pct: number }[];
  relation_distribution: { label: string; count: number; pct: number }[];
}

export interface BackendPaginatedResult<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  scope_mode?: string;
  scope_warning?: string;
}

export type CompiledAttrType = 'string' | 'text' | 'number' | 'date' | 'enum' | 'boolean';
export type CompiledRelationCardinality = 'one_to_one' | 'one_to_many' | 'many_to_many' | 'custom';
export type SchemaMode = 'template_seeded' | 'manual_edited';
export type SchemaSyncStatus = 'synced' | 'outdated';

export interface CompiledSchemaAttribute {
  name: string;
  display_name: string;
  description?: string;
  type: CompiledAttrType;
  required: boolean;
  is_primary_key?: boolean;
  source_attribute_id?: string | null;
}

export interface CompiledSchemaEntityType {
  name: string;
  display_name: string;
  description?: string;
  requirement?: string;
  status?: 'active' | 'inactive';
  aliases: string[];
  source_object_id?: string | null;
  attributes: CompiledSchemaAttribute[];
}

export interface CompiledSchemaRelationType {
  name: string;
  display_name: string;
  description?: string;
  status?: 'active' | 'inactive';
  aliases: string[];
  source: string;
  target: string;
  source_relation_id?: string | null;
  cardinality: CompiledRelationCardinality;
}

export interface CompiledSchema {
  id?: number;
  tenant_id?: string;
  knowledge_base_id?: string;
  template_domain_id?: string | null;
  template_scenario_id?: string | null;
  source_type?: string;
  source_version: number;
  source_hash?: string | null;
  schema_version: number;
  entity_types: CompiledSchemaEntityType[];
  relation_types: CompiledSchemaRelationType[];
  constraints: Record<string, unknown>[];
  disambiguation: Record<string, unknown>;
  prompt_schema: Record<string, unknown>;
  status: string;
  schema_mode: SchemaMode;
  sync_status: SchemaSyncStatus;
  edited_at?: string | null;
  edited_by?: string | null;
  compiled_at?: string | null;
}

export interface OntologyBinding {
  id: number;
  tenant_id: string;
  knowledge_base_id: string;
  template_domain_id?: string | null;
  template_scenario_id?: string | null;
  source_type: string;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface OntologyTemplateSummary {
  domain_id?: string | null;
  domain_name?: string | null;
  scenario_id?: string | null;
  scenario_name?: string | null;
  source_version?: number | null;
  source_hash?: string | null;
}

export interface OntologyEditorState {
  knowledge_base_id: string;
  binding?: OntologyBinding | null;
  compiled_schema?: CompiledSchema | null;
  current_template?: OntologyTemplateSummary | null;
}

// ─── 编译引擎设置：本体对象/关系/编译步骤/引擎设置 ───────────────

export type OntologyDefStatus = 'active' | 'inactive';

export function getOntologyStatusTextMap(t: (key: string) => string): Record<OntologyDefStatus, string> {
  return {
    active: t('status.active'),
    inactive: t('status.inactive'),
  };
}

/** OntologyDefStatus → translation key */
export const ontologyStatusLabelKey: Record<OntologyDefStatus, string> = {
  active: 'status.active',
  inactive: 'status.inactive',
};

export type OntologyAttrType = '字符串' | '数值' | '日期' | '枚举' | '文本' | '布尔';

export interface OntologyAttribute {
  id: string;
  name: string;
  description?: string;
  type: OntologyAttrType;
  isPrimaryKey: boolean;
}

export interface OntologyObjectDef {
  id: string;
  knowledgeBaseId: string;
  name: string;
  description: string;
  attributes: OntologyAttribute[];
  requirement: string;
  status: OntologyDefStatus;
}

export type OntologyRelationType = '一对一' | '一对多' | '多对一' | '多对多' | '自定义';

export interface OntologyRelationDef {
  id: string;
  knowledgeBaseId: string;
  sourceObject: string;
  name: string;
  targetObject: string;
  description: string;
  relationType: OntologyRelationType;
  status: OntologyDefStatus;
}

export type CompileScope = 'single' | 'whole';
export type CompileTrigger = 'upload' | 'update';

export function getCompileScopeTextMap(t: (key: string) => string): Record<CompileScope, string> {
  return {
    single: t('compile.scope.single'),
    whole: t('compile.scope.whole'),
  };
}

/** CompileScope → translation key */
export const compileScopeLabelKey: Record<CompileScope, string> = {
  single: 'compile.scope.single',
  whole: 'compile.scope.whole',
};

export function getCompileTriggerTextMap(t: (key: string) => string): Record<CompileTrigger, string> {
  return {
    upload: t('compile.trigger.upload'),
    update: t('compile.trigger.update'),
  };
}

/** CompileTrigger → translation key */
export const compileTriggerLabelKey: Record<CompileTrigger, string> = {
  upload: 'compile.trigger.upload',
  update: 'compile.trigger.update',
};

export interface CompileStep {
  id: string;
  knowledgeBaseId: string;
  order: number;
  name: string;
  prompt: string;
  skill: string;
  scope: CompileScope;
  trigger: CompileTrigger;
  template: string;
}

export interface EngineSetting {
  knowledgeBaseId: string;
  semanticModel: string;
}

export type SaveOntologyObjectPayload = Omit<OntologyObjectDef, 'id' | 'knowledgeBaseId'>;
export type SaveOntologyRelationPayload = Omit<OntologyRelationDef, 'id' | 'knowledgeBaseId'>;
export type SaveCompileStepPayload = Omit<CompileStep, 'id' | 'knowledgeBaseId'>;

// ─── 本体约束定义 ───────────────────────────────────────────────

export type ConstraintTargetType = 'entity' | 'attribute' | 'relation';

export function getConstraintTargetTypeTextMap(t: (key: string) => string): Record<ConstraintTargetType, string> {
  return {
    entity: t('compile.constraintTargetType.entity'),
    attribute: t('compile.constraintTargetType.attribute'),
    relation: t('compile.constraintTargetType.relation'),
  };
}

/** ConstraintTargetType → translation key */
export const constraintTargetTypeLabelKey: Record<ConstraintTargetType, string> = {
  entity: 'compile.constraintTargetType.entity',
  attribute: 'compile.constraintTargetType.attribute',
  relation: 'compile.constraintTargetType.relation',
};

/** compiled_schema.constraints 中的持久化结构（snake_case，与后端一致）。 */
export interface CompiledSchemaConstraint {
  name: string;
  target_type: ConstraintTargetType;
  target_code: string;
  target_label?: string | null;
  constraint_type: string;
  expression?: string;
  suggestion?: string;
}

/** 前端 UI 约束模型（camelCase）。name 即唯一键。 */
export interface OntologyConstraint {
  id: string;
  name: string;
  targetType: ConstraintTargetType;
  targetCode: string;
  targetLabel?: string;
  constraintType: string;
  expression?: string;
  suggestion?: string;
}

export type SaveOntologyConstraintPayload = Omit<OntologyConstraint, 'id'>;

// ─── 同义词组（KB 级） ─────────────────────────────────────────

export interface SynonymGroup {
  id: string;
  knowledgeBaseId: string;
  terms: string[];
  canonical?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface SynonymListResult {
  items: SynonymGroup[];
  total: number;
  page: number;
  pageSize: number;
}

export interface SynonymImportResult {
  created: number;
  skipped: number;
  failed: { index: number; reason: string }[];
}

export interface YamlImportCounts {
  entities: number;
  attributes: number;
  relations: number;
  constraints: number;
}

export interface YamlImportResult {
  dry_run?: boolean;
  counts?: YamlImportCounts;
  errors?: string[];
  schema_version?: number;
  status?: string;
}

// ── OpenKB Wiki 只读视图（parse-results/* 数据源）──

/** Wiki 实体/概念行（GET parse-results/entities） */
export interface WikiEntityRow {
  id: string;
  name: string;
  /** OpenKB 自由生成的类型，如 Work / Organization；非本体 schema 的 entity_type */
  type: string;
  description: string;
  /** 该实体在 wikilinks 图中的度数 */
  relationsCount: number;
}

/** Wiki 关系行（GET parse-results/relationships） */
export interface WikiRelationshipRow {
  id: string;
  source: string;
  target: string;
  description: string;
}

/** Wiki 图谱数据（GET parse-results/graph） */
export interface WikiGraphData {
  nodes: { id: string; name: string; type: string; description: string }[];
  edges: { id: string; source: string; target: string }[];
}

// ── [jonex] Wiki 阅读模式（编译结果页）──

/** Wiki 页面内容（GET parse-results/wiki-page） */
export interface WikiPageContent {
  path: string;
  section: 'summaries' | 'concepts' | 'entities';
  name: string;
  title: string;
  type: string;
  description: string;
  sources: string[];
  content: string;
}

/** Wiki 页面树条目（后端已结构化，含显示名——不是裸 stem） */
export interface WikiPageItem {
  stem: string;
  title: string;
  type: string;
  description: string;
}

/** Wiki 页面树（GET parse-results/wiki-contents；字段已 camelCase 映射） */
export interface WikiContents {
  summaries: WikiPageItem[];
  concepts: WikiPageItem[];
  entities: WikiPageItem[];
  documentId: string;
}
