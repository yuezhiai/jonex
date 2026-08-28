import type {
  DomainKnowledgeSpace,
  DomainKnowledgeListParams,
  DomainKnowledgeStatus,
  DomainKnowledgeSourceType,
  PaginationResult,
  DomainKnowledgeItem,
  DomainKnowledgePermissionMember,
  DomainKnowledgePermissionPayload,
  DomainKnowledgeDetail,
  DataSourceConfig,
  ParserFileConfig,
  OntologyTemplate,
  ValidationRule,
  PromptTemplate,
  CompileEntity,
  EntityListParams,
  EntityDistribution,
  RelationDistribution,
  LogicRule,
  ActionRule,
  DomainKnowledgeResultStats,
  GraphSummary,
  DocumentChunk,
  ManualDocItem,
  ManualDocListParams,
  DocumentStatsResult,
  BackendParseResultSummary,
  BackendEntityItem,
  BackendGraphSummary,
  BackendPaginatedResult,
  CompiledSchema,
  CompiledSchemaEntityType,
  CompiledSchemaRelationType,
  OntologyEditorState,
  OntologyObjectDef,
  OntologyRelationDef,
  CompileStep,
  EngineSetting,
  SaveOntologyObjectPayload,
  SaveOntologyRelationPayload,
  SaveCompileStepPayload,
  OntologyConstraint,
  SaveOntologyConstraintPayload,
  CompiledSchemaConstraint,
  OntologyInstanceSummary,
  RelationInstanceSummary,
  OntologyInstanceRow,
  OntologyInstanceListParams,
  RelationInstanceRow,
  RelationInstanceListParams,
  OntologyStatistics,
  OntologyGraphData,
  OntologyGraphParams,
  OntologyNeighborData,
  FolderItem,
  FolderListResponse,
  CreateOntologyInstanceRequest,
  CreateOntologyInstanceResponse,
  CreateOntologyRelationResponse,
  UpdateOntologyInstanceResponse,
  DeleteOntologyInstanceResponse,
  UpdateOntologyRelationResponse,
  DeleteOntologyRelationResponse,
  YamlImportResult,
  KnowledgeBaseType,
  WikiEntityRow,
  WikiRelationshipRow,
  WikiGraphData,
  WikiPageContent,
  WikiPageItem,
  WikiContents,
} from '@/types/domainKnowledge';
import { request, getData, postData, putData, deleteData } from './request';
import axios from 'axios';
import { readAccessToken } from '@jonex/shell-sdk';
import { listDataSources } from './dataSource';
import type { DataSourceInstance } from '@/types/dataSource';
import { dataSourceInstanceDisplayName } from '@/utils/dataSourceDisplay';

export function getDomainKnowledgeSpaces(): Promise<DomainKnowledgeSpace[]> {
  return getData<{ items: DomainKnowledgeSpace[] }>(
    request.get('/knowledge-base/spaces', { params: { limit: 100 } }),
  ).then((res) => res.items);
}

// ══ 系统配额（GET /knowledge-base/quota，契约见配额需求文档）══

/** 单条配额项：bytes 类（*SizeLimitMiB / *StorageLimitMiB）used/limit 为 Byte，unit 仅作展示标签 */
export interface QuotaItem {
  quotaKey: string;
  used: number;
  reserved: number;
  limit: number;
  unit: string;
}

export interface QuotaViewResponse {
  quotas: QuotaItem[];
}

/** 查询租户配额视图；传入 knowledge_base_id 时 knowledgeBaseDocumentLimit.used 返回该 KB 真实值 */
export function getQuotaView(knowledgeBaseId?: string): Promise<QuotaViewResponse> {
  return getData<QuotaViewResponse>(
    request.get('/knowledge-base/quota', {
      params: { knowledge_base_id: knowledgeBaseId || undefined },
    }),
  );
}

export function getDomainKnowledgeList(
  params: DomainKnowledgeListParams,
): Promise<PaginationResult<DomainKnowledgeItem>> {
  const query: Record<string, string | number | undefined> = {
    keyword: params.keyword,
    space_id: params.spaceId,
    status: params.status,
    source_type: params.sourceType,
    offset: (params.page - 1) * params.pageSize,
    limit: params.pageSize,
    sort_field: params.sortField,
    sort_order: params.sortOrder,
  };
  // 过滤掉 undefined 值
  const cleanQuery: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== '') {
      cleanQuery[k] = v;
    }
  }
  return getData<BackendKBListResponse>(request.get('/knowledge-base/knowledge-info', { params: cleanQuery })).then(
    mapKBList,
  );
}

interface BackendKBItem {
  id: string;
  tenant_id: string;
  space_id: string;
  space_name?: string;
  name: string;
  description: string | null;
  data_source_types: string[];
  document_count: number;
  status: string;
  owner_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  // ── 详情接口集成的本体 kb 维度统计（仅 get 详情返回，列表不含）──
  entity_count?: number;
  relation_count?: number;
  /** Neo4j 不可用时为 true，本体计数降级为 0（基础知识库能力不受影响） */
  ontology_degraded?: boolean;
  /** [jonex] 知识库类型：lightrag / openkb（仅 get 详情返回） */
  kb_type?: string;
}

interface BackendKBListResponse {
  items: BackendKBItem[];
  total: number;
  offset: number;
  limit: number;
}

function mapKBList(resp: BackendKBListResponse): PaginationResult<DomainKnowledgeItem> {
  const list = resp.items.map(mapKBItem);
  const pageSize = resp.limit || list.length;
  const page = pageSize > 0 ? Math.floor((resp.offset || 0) / pageSize) + 1 : 1;
  return { list, pagination: { page, pageSize, total: resp.total } };
}

/** 创建知识库 */
export async function createKnowledgeInfo(data: {
  name: string;
  space_id: string;
  description?: string;
  data_source_types?: string[];
  /** [jonex] 知识库类型，创建时选定后不可变；缺省由后端落 lightrag */
  kb_type?: KnowledgeBaseType;
}): Promise<DomainKnowledgeItem> {
  const backendItem = await getData<BackendKBItem>(request.post('/knowledge-base/knowledge-info', data));
  return mapKBItem(backendItem);
}

/** 更新知识库 */
export async function updateKnowledgeInfo(
  kbId: string,
  data: {
    name?: string;
    space_id?: string;
    description?: string;
    status?: string;
  },
): Promise<DomainKnowledgeItem> {
  const backendItem = await getData<BackendKBItem>(request.patch(`/knowledge-base/knowledge-info/${kbId}`, data));
  return mapKBItem(backendItem);
}

/** 删除知识库 */
export async function deleteKnowledgeInfo(kbId: string): Promise<void> {
  await getData(request.delete(`/knowledge-base/knowledge-info/${kbId}`));
}

function mapKBItem(item: BackendKBItem): DomainKnowledgeItem {
  return {
    id: item.id,
    name: item.name,
    spaceId: item.space_id,
    spaceName: item.space_name || '',
    dataSourceTypes: item.data_source_types as DomainKnowledgeSourceType[],
    documentCount: item.document_count || 0,
    status: item.status as DomainKnowledgeStatus,
    updatedAt: item.updated_at ? formatLocalDateTime(item.updated_at) : '—',
    ownerName: item.owner_id || undefined,
    description: item.description || undefined,
    // [jonex] 知识库类型：列表/创建/更新响应都带，编辑弹窗与列表标签依赖它
    kbType: (item.kb_type as KnowledgeBaseType) || 'lightrag',
    // 权限字段（KB 权限：管理权 + 写权限 + 授权共享标识）
    can_manage_permissions: (item as unknown as Record<string, unknown>).can_manage_permissions === true,
    can_write: (item as unknown as Record<string, unknown>).can_write === true,
    grant_shared: (item as unknown as Record<string, unknown>).grant_shared === true,
  };
}

/** KB 授权成员（后端 kb_permissions 表；display_name 后端 join，前端不再拉 /users 拼名字） */
interface BackendKbPermission {
  user_id: string;
  role: 'viewer' | 'editor' | 'kb_manager';
  display_name: string | null;
  created_at: string | null;
}

export async function getDomainKnowledgePermissions(
  knowledgeBaseId: string,
  keyword?: string,
): Promise<{ knowledgeBaseId: string; members: DomainKnowledgePermissionMember[] }> {
  const result = await getData<{ permissions: BackendKbPermission[] }>(
    request.get(`/knowledge-base/knowledge-info/${knowledgeBaseId}/permissions`),
  );
  let members: DomainKnowledgePermissionMember[] = (result.permissions ?? []).map((p) => ({
    userId: p.user_id,
    name: p.display_name || p.user_id.slice(0, 8),
    dept: '',
    avatarText: (p.display_name || p.user_id).charAt(0).toUpperCase(),
    avatarColor: '#94a3b8',
    role: p.role === 'kb_manager' ? 'kb_manager' : p.role === 'editor' ? 'editor' : 'viewer',
  }));
  if (keyword) members = members.filter((m) => m.name.includes(keyword));
  return { knowledgeBaseId, members };
}

export async function saveDomainKnowledgePermissions(
  knowledgeBaseId: string,
  payload: DomainKnowledgePermissionPayload,
): Promise<boolean> {
  await getData(
    request.put(`/knowledge-base/knowledge-info/${knowledgeBaseId}/permissions`, {
      permissions: payload.members.map((m) => ({ user_id: m.userId, role: m.role })),
    }),
  );
  return true;
}

// ─── Detail APIs ────────────────────────────────────────

export async function getDomainKnowledgeDetail(kbId: string): Promise<DomainKnowledgeDetail> {
  // 详情 header 由知识库详情接口一次取齐：
  //   GET /knowledge-base/knowledge-info/{kbId}
  // 该接口已集成本体 kb 维度统计（entity_count / relation_count / ontology_degraded），
  // 同时返回 空间名 / 状态 / 文档数 / 更新时间，无需再叠加额外请求。
  // 注意：ontology_degraded 为 true 时 Neo4j 不可用，实体/关系数被降级为 0（非真实为 0）。
  const info = await getData<BackendKBItem>(request.get(`/knowledge-base/knowledge-info/${kbId}`));
  return {
    id: info.id,
    name: info.name,
    spaceId: info.space_id,
    spaceName: info.space_name || '',
    documentCount: info.document_count ?? 0,
    entityCount: info.entity_count ?? 0,
    relationCount: info.relation_count ?? 0,
    compileVersionCount: 0,
    status: (info.status as DomainKnowledgeStatus) || 'synced',
    updatedAt: info.updated_at ? formatLocalDateTime(info.updated_at) : '—',
    ontologyDegraded: info.ontology_degraded ?? false,
    kbType: (info.kb_type as KnowledgeBaseType) || 'lightrag',
    can_manage_permissions: (info as unknown as Record<string, unknown>).can_manage_permissions === true,
    can_write: (info as unknown as Record<string, unknown>).can_write === true,
  };
}

export function getDomainKnowledgeDataSources(kbId: string, t?: (key: string) => string): Promise<DataSourceConfig[]> {
  const _t = t || ((key: string) => key);
  return listDataSources(kbId).then((list) =>
    list.map((ds) => {
      const v = getDSView(_t)[ds.accessType] || getDSView(_t).file;
      return {
        id: ds.id,
        name: dataSourceInstanceDisplayName(ds, _t),
        type: v.typeLabel,
        accessType: ds.accessType,
        configJson: ds.configJson || {},
        docs: ds.documentCount,
        status: dsStatusLabel(ds, _t),
        desc: dsDesc(ds, _t),
        iconType: v.iconType,
        iconBg: v.iconBg,
        iconColor: v.iconColor,
        path:
          ds.accessType === 'file'
            ? `/domain-knowledge/${kbId}/datasource/manual`
            : `/domain-knowledge/${kbId}/${v.routePrefix}/${ds.id}`,
        knowledgeBaseId: kbId,
      };
    }),
  );
}

function getDSView(t: (key: string) => string): Record<
  string,
  {
    iconType: DataSourceConfig['iconType'];
    iconBg: string;
    iconColor: string;
    typeLabel: string;
    routePrefix: string;
  }
> {
  return {
    api: {
      iconType: 'api',
      iconBg: '#ecfdf5',
      iconColor: '#10b981',
      typeLabel: t('domainKnowledge.dataSourceType.api'),
      routePrefix: 'datasource/sync',
    },
    storage: {
      iconType: 'storage',
      iconBg: '#fff7ed',
      iconColor: '#f97316',
      typeLabel: t('domainKnowledge.dataSourceType.storage'),
      routePrefix: 'datasource/storage',
    },
    api_push: {
      iconType: 'api',
      iconBg: '#eff6ff',
      iconColor: '#3b82f6',
      typeLabel: t('domainKnowledge.dataSourceType.apiPush'),
      routePrefix: 'datasource/api-push',
    },
    file: {
      iconType: 'upload',
      iconBg: '#eff6ff',
      iconColor: '#3b82f6',
      typeLabel: t('domainKnowledge.dataSourceType.file'),
      routePrefix: 'datasource/manual',
    },
  };
}

function dsDesc(ds: DataSourceInstance, t: (key: string) => string): string {
  const c = ds.configJson || {};
  if (ds.accessType === 'api') return `${t('domainKnowledge.dataSourceDesc.endpoint')}：${c.endpoint || '-'}`;
  if (ds.accessType === 'storage')
    return `${c.backend || ''} ｜ ${t('domainKnowledge.dataSourceDesc.bucket')}：${c.bucket || '-'}${c.prefix ? ' / ' + c.prefix : ''}`;
  if (ds.accessType === 'api_push')
    return `${t('domainKnowledge.dataSourceDesc.apiPush')} ｜ ${t('domainKnowledge.dataSourceDesc.allowed')}：${(c.allowed_ext || []).join('/') || '-'}`;
  return t('domainKnowledge.dataSourceDesc.manualUpload');
}

function dsStatusLabel(ds: DataSourceInstance, t: (key: string) => string): string {
  if (ds.status === 'paused') return t('domainKnowledge.dataSourceStatus.paused');
  if (ds.status === 'error' || ds.lastSyncStatus === 'failed') return t('domainKnowledge.dataSourceStatus.failed');
  return t('domainKnowledge.dataSourceStatus.running');
}

export function getDomainKnowledgeParserConfigs(kbId: string): Promise<ParserFileConfig[]> {
  return getData(request.get(`/core-business/domain-knowledge/${kbId}/parser-configs`));
}

export function getDomainKnowledgeOntologyTemplates(kbId: string): Promise<OntologyTemplate[]> {
  return getData(request.get(`/core-business/domain-knowledge/${kbId}/ontology-templates`));
}

export function getDomainKnowledgeValidationRules(kbId: string): Promise<ValidationRule[]> {
  return getData(request.get(`/core-business/domain-knowledge/${kbId}/validation-rules`));
}

export function getDomainKnowledgePrompts(kbId: string): Promise<PromptTemplate[]> {
  return getData(request.get(`/core-business/domain-knowledge/${kbId}/prompts`));
}

export function getDomainKnowledgeEntities(params: EntityListParams): Promise<PaginationResult<CompileEntity>> {
  // if (useMock) return mockGetDomainKnowledgeEntities(params)

  const query: Record<string, string | number> = {
    knowledge_base_id: params.knowledgeBaseId,
    page: params.page,
    page_size: params.pageSize,
  };
  if (params.keyword) query.keyword = params.keyword;
  if (params.entityType) query.entity_type = params.entityType;

  return getData<BackendPaginatedResult<BackendEntityItem>>(
    request.get(`/knowledge-base/parse-results/entities`, { params: query }),
  ).then((res) => ({
    list: res.items.map(mapEntityItem),
    pagination: { page: res.page, pageSize: res.page_size, total: res.total },
  }));
}

// entity/relation distribution now derived from graph-summary cache (see below)

export function getDomainKnowledgeLogicRules(kbId: string): Promise<LogicRule[]> {
  return getData(request.get(`/core-business/domain-knowledge/${kbId}/logic-rules`));
}

export function getDomainKnowledgeActionRules(kbId: string): Promise<ActionRule[]> {
  return getData(request.get(`/core-business/domain-knowledge/${kbId}/action-rules`));
}

export function getDomainKnowledgeResultStats(kbId: string): Promise<DomainKnowledgeResultStats> {
  // if (useMock) return mockGetDomainKnowledgeResultStats(kbId)
  return getData<BackendParseResultSummary>(
    request.get(`/knowledge-base/parse-results/summary`, {
      params: { knowledge_base_id: kbId },
    }),
  ).then(mapResultStats);
}

// ── graph summary (real endpoint, cached for distribution derivation) ──

let _graphSummaryCache: BackendGraphSummary | null = null;

export async function getDomainKnowledgeGraphSummary(kbId: string): Promise<GraphSummary> {
  // if (useMock) return mockGetDomainKnowledgeGraphSummary()
  const backend = await getData<BackendGraphSummary>(
    request.get(`/knowledge-base/parse-results/graph-summary`, {
      params: { knowledge_base_id: kbId },
    }),
  );
  _graphSummaryCache = backend;
  return mapGraphSummary(backend);
}

export function getDomainKnowledgeEntityDistribution(): Promise<EntityDistribution[]> {
  // if (useMock) return mockGetDomainKnowledgeEntityDistribution()
  if (_graphSummaryCache) return Promise.resolve(deriveEntityDistribution(_graphSummaryCache));
  return Promise.resolve([]);
}

export function getDomainKnowledgeRelationDistribution(): Promise<RelationDistribution[]> {
  // if (useMock) return mockGetDomainKnowledgeRelationDistribution()
  if (_graphSummaryCache) return Promise.resolve(deriveRelationDistribution(_graphSummaryCache));
  return Promise.resolve([]);
}

export function getOntologyEditorState(kbId: string): Promise<OntologyEditorState> {
  return getData<OntologyEditorState>(
    request.get('/knowledge-base/ontology/editor-state', {
      params: { knowledge_base_id: kbId },
    }),
  );
}

export function saveOntologyCompiledSchema(
  kbId: string,
  entityTypes: CompiledSchemaEntityType[],
  relationTypes: CompiledSchemaRelationType[],
  expectedSchemaVersion: number,
  constraints?: CompiledSchemaConstraint[],
): Promise<CompiledSchema> {
  return putData<CompiledSchema>('/knowledge-base/ontology/compiled-schema', {
    knowledge_base_id: kbId,
    entity_types: entityTypes,
    relation_types: relationTypes,
    // 乐观锁：始终携带读取时的版本，后端不匹配返回 409
    expected_schema_version: expectedSchemaVersion,
    // 约束三态：undefined=不传（后端保留现有）；数组=替换/清空
    ...(constraints !== undefined ? { constraints } : {}),
  });
}

export function reseedOntologyCompiledSchema(
  kbId: string,
  templateScenarioId: string,
  templateDomainId?: string,
): Promise<CompiledSchema> {
  return postData<CompiledSchema>('/knowledge-base/ontology/compiled-schema/reseed', {
    knowledge_base_id: kbId,
    template_domain_id: templateDomainId,
    template_scenario_id: templateScenarioId,
    source_type: 'business_template',
  });
}

// ─── Manual Datasource APIs ─────────────────────────────

interface BackendDocItem {
  id: string;
  file_name: string;
  file_type?: string;
  file_size?: number;
  status: string;
  ontology_status?: string;
  error_message?: string;
  ontology_error?: string;
  created_at?: string;
  updated_at?: string;
  /** [jonex] 后端 to_dict 输出键名是 metadata（非 extra_metadata）——历史 bug 修正。 */
  metadata?: Record<string, unknown>;
  /** [jonex] LLM-Wiki 编译状态（顶层字段，migration 008 列化）。 */
  llm_wiki_compile_status?: string;
  llm_wiki_compile_error?: string;
  data_source_type?: string;
  mime_type?: string;
  folder_id?: string | null;
}

interface BackendDocListResponse {
  items: BackendDocItem[];
  total: number;
  page: number;
  page_size: number;
}

function formatFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function formatLocalDateTime(isoStr: string): string {
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function mapBackendDoc(doc: BackendDocItem, knowledgeBaseId: string, _t: (key: string) => string): ManualDocItem {
  // [jonex] 修正历史 bug：后端 to_dict 输出键是 metadata（非 extra_metadata），
  // 此前 meta 恒为空对象（uploader 等字段全空）。顺带提取 OpenKB 编译状态。
  const meta = doc.metadata || {};
  return {
    id: doc.id,
    name: doc.file_name,
    type: (doc.file_type || (doc.file_name || '').split('.').pop() || 'unknown').toUpperCase(),
    size: typeof doc.file_size === 'number' ? formatFileSize(doc.file_size) : '-',
    uploader: (meta.uploader as string) || '-',
    uploadTime: doc.created_at ? formatLocalDateTime(doc.created_at) : '-',
    // status 保留旧展示字符串以兼容其他调用点；新的状态列改用 docStatus/ontologyStatus 派生 phase。
    status:
      doc.status === 'ready'
        ? '入库·解析·编译'
        : doc.status === 'parsing'
          ? '入库·解析中'
          : doc.status === 'pending'
            ? '入库'
            : doc.status,
    docStatus: doc.status,
    ontologyStatus: doc.ontology_status,
    errorMessage: doc.error_message,
    ontologyError: doc.ontology_error,
    // [jonex] LLM-Wiki 编译状态（顶层字段；metadata 兼容旧 JSONB 读点）
    llmWikiCompileStatus: doc.llm_wiki_compile_status || (meta.openkb_compile_status as string) || undefined,
    llmWikiCompileError: doc.llm_wiki_compile_error || (meta.openkb_compile_error as string) || undefined,
    // [jonex] 管线类型：metadata.kb_type（upload/reparse 时写入）——与 doc 详情同一次请求到达
    kbType: (meta.kb_type as KnowledgeBaseType) || undefined,
    knowledgeBaseId,
    dataSourceType: doc.data_source_type,
    mimeType: doc.mime_type,
    folder_id: doc.folder_id ?? null,
    mediaType: doc.mime_type && doc.mime_type.includes('video') ? 'video' : 'document',
  };
}

export async function getManualDocList(
  params: ManualDocListParams,
  t?: (key: string) => string,
): Promise<PaginationResult<ManualDocItem>> {
  const _t = t || ((key: string) => key);
  // if (useMock) return mockGetManualDocList(params)  // 对接后端接口，不使用 mock

  const backendParams: Record<string, string | number> = {
    knowledge_base_id: params.knowledgeBaseId,
    page: params.page,
    page_size: params.pageSize,
  };
  if (params.status) backendParams.status = params.status;
  if (params.ontology_status) backendParams.ontology_status = params.ontology_status;
  if (params.keyword) backendParams.keyword = params.keyword;
  if (params.folder_id) backendParams.folder_id = params.folder_id;

  // phase 多值：后端期望 phase=a&phase=b（FastAPI list[str] Query），
  // 用 indexes:null 让 axios 序列化为无下标的重复键。
  const hasPhase = Array.isArray(params.phase) && params.phase.length > 0;
  const finalParams: Record<string, unknown> = hasPhase ? { ...backendParams, phase: params.phase } : backendParams;

  const backendResult = await getData<BackendDocListResponse>(
    request.get('/knowledge-base/documents', {
      params: finalParams,
      paramsSerializer: { indexes: null },
    }),
  );

  // 后端已支持 keyword 过滤（DocumentListRequest.keyword，对 file_name/file_path 做 ilike），
  // 且返回真实 total（repo.count），分页直接采用后端结果，不再做客户端二次过滤。
  const list = backendResult.items.map((d) => mapBackendDoc(d, params.knowledgeBaseId, _t));
  return {
    list,
    pagination: {
      page: backendResult.page ?? params.page,
      pageSize: backendResult.page_size ?? params.pageSize,
      total: backendResult.total ?? list.length,
    },
  };
}

export interface UploadDocumentOptions {
  onUploadProgress?: (e: { loaded: number; total?: number }) => void;
  signal?: AbortSignal;
}

export async function uploadManualDocument(
  knowledgeBaseId: string,
  file: File,
  folderId?: string,
  t?: (key: string) => string,
  options?: UploadDocumentOptions,
): Promise<ManualDocItem> {
  const _t = t || ((key: string) => key);
  // if (useMock) return mockUploadManualDocument(knowledgeBaseId, file)  // 对接后端接口，不使用 mock

  const formData = new FormData();
  formData.append('file', file);
  formData.append('knowledge_base_id', knowledgeBaseId);
  if (folderId) {
    formData.append('folder_id', folderId);
  }

  // [jonex] R7: 上传接口单独放大 timeout 到 300s，避免大文件触发全局 30s 超时
  const result = await getData<BackendDocItem>(
    request.post('/knowledge-base/documents/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,  // 5 min
      onUploadProgress: options?.onUploadProgress,
      signal: options?.signal,
    }),
  );

  return mapBackendDoc(result, knowledgeBaseId, _t);
}

export async function getManualDocumentDetail(
  knowledgeBaseId: string,
  documentId: string,
  t?: (key: string) => string,
): Promise<ManualDocItem> {
  const _t = t || ((key: string) => key);
  const doc = await getData<BackendDocItem>(
    request.get(`/knowledge-base/documents/${documentId}`, {
      params: { knowledge_base_id: knowledgeBaseId },
    }),
  );
  return mapBackendDoc(doc, knowledgeBaseId, _t);
}

export async function getDocumentChunks(documentId: string): Promise<DocumentChunk[]> {
  const res = await getData<{
    doc_id: string
    total: number
    chunks: DocumentChunk[]
  }>(request.get(`/knowledge-base/documents/${documentId}/chunks`))
  return (res.chunks ?? []).map((c) => ({
    ...c,
    // 兼容映射：旧代码通过 content_summary/content_length 读内容
    content_summary: c.content ?? c.content_summary,
    content_length: c.content?.length ?? c.content_length ?? 0,
  }))
}

export async function deleteManualDocument(knowledgeBaseId: string, documentId: string): Promise<void> {
  await getData(
    request.delete(`/knowledge-base/documents/${documentId}`, {
      params: { knowledge_base_id: knowledgeBaseId },
    }),
  );
}

/**
 * 重抽本体（ontology-only）：不重解析文件、不重推 chunk，
 * 只按当前 compiled schema 对该文档已入库的实体/关系重新归类。
 * 后端 POST /documents/{id}/retry-ontology 必须携带 knowledge_base_id。
 */
export async function retryDocumentOntology(knowledgeBaseId: string, documentId: string): Promise<void> {
  await postData(`/knowledge-base/documents/${documentId}/retry-ontology`, {
    document_id: documentId,
    knowledge_base_id: knowledgeBaseId,
  });
}

/** 重新解析文档 */
export async function reparseDocument(documentId: string): Promise<void> {
  await postData(`/knowledge-base/documents/${documentId}/reparse`, {
    document_id: documentId,
  });
}

interface BackendDocumentStats {
  total: number;
  processing: number;
  completed: number;
  compile_failed: number;
  parse_failed: number;
}

/** 文档处理状态统计（GET /knowledge-base/documents/stats）。互斥四桶，相加 = total。 */
export async function getDocumentStats(knowledgeBaseId: string): Promise<DocumentStatsResult> {
  const s = await getData<BackendDocumentStats>(
    request.get('/knowledge-base/documents/stats', {
      params: { knowledge_base_id: knowledgeBaseId },
    }),
  );
  return {
    total: s.total ?? 0,
    processing: s.processing ?? 0,
    completed: s.completed ?? 0,
    compileFailed: s.compile_failed ?? 0,
    parseFailed: s.parse_failed ?? 0,
  };
}

const RAW_API_BASE = (import.meta as any).env?.VITE_API_BASE_URL || '/api/v1';

/**
 * 获取文档原文、可直接在浏览器打开的 Blob URL。
 * 后端 GET /documents/{id}/raw：COS 后端 302 跳预签名 URL，local 后端代理文件字节。
 * 使用带鉴权的裸 axios + responseType blob，避开统一 envelope 响应拦截器。
 * 调用方用完后应 URL.revokeObjectURL 释放。
 */
export async function fetchManualDocumentRawUrl(documentId: string): Promise<string> {
  const token = readAccessToken();
  const resp = await axios.get(`${RAW_API_BASE}/knowledge-base/documents/${documentId}/raw`, {
    responseType: 'blob',
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  let blob = resp.data as Blob;
  // 文本类统一按 UTF-8 重封装为 text/plain;charset=utf-8：
  // Chrome 对 blob: URL 的 text/markdown 等子类型会不稳定地忽略 charset 导致中文乱码，
  // 而 Blob.text() 一律按 UTF-8 解码，再重建即可确保正确显示；二进制类型保持原样以便原生预览。
  const baseType = (blob.type || '').split(';')[0].trim().toLowerCase();
  const isText =
    baseType.startsWith('text/') ||
    baseType === 'application/json' ||
    baseType === 'application/xml' ||
    baseType === 'application/javascript';
  if (isText) {
    const text = await blob.text();
    blob = new Blob([text], { type: 'text/plain; charset=utf-8' });
  }
  return URL.createObjectURL(blob);
}

/**
 * 获取文档原文的短时查看票据 URL（音视频直连流式 + PDF/图片新标签内联）。
 * 返回的 url 形如 /api/v1/knowledge-base/documents/{id}/raw?token=...，可直接喂给
 * `<video src>` / `window.open`，浏览器自带 Range 请求即可边下边播、拖动 seek。
 */
export async function getDocumentViewTicket(documentId: string): Promise<{ url: string; expiresIn: number }> {
  const res = await getData<{ url: string; expires_in: number }>(
    request.get(`/knowledge-base/documents/${documentId}/view-ticket`),
  );
  return { url: res.url, expiresIn: res.expires_in };
}

// ── Parse Result Mappers ─────────────────────────────────

const DIST_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#f97316'];

function mapResultStats(b: BackendParseResultSummary): DomainKnowledgeResultStats {
  return {
    entityCount: b.entities_count,
    relationCount: b.relationships_count,
    compileVersionCount: b.compile_versions_count,
    sourceFileCount: b.documents_count,
  };
}

function mapEntityItem(b: BackendEntityItem): CompileEntity {
  return {
    id: b.id,
    name: b.name,
    type: b.type,
    attrs: 0,
    relations: b.relations_count,
    createdAt: b.created_at ? formatLocalDateTime(new Date(b.created_at * 1000).toISOString()) : '-',
    knowledgeBaseId: '',
  };
}

function mapGraphSummary(b: BackendGraphSummary): GraphSummary {
  return {
    entityTypeCount: b.entity_type_count,
    relationTotalCount: b.edges_count,
    relationTypeCount: b.relation_type_count,
    avgDegree: b.avg_degree,
  };
}

function deriveEntityDistribution(b: BackendGraphSummary): EntityDistribution[] {
  return b.entity_type_distribution.map((d, i) => ({
    ...d,
    color: DIST_COLORS[i % DIST_COLORS.length],
  }));
}

function deriveRelationDistribution(b: BackendGraphSummary): RelationDistribution[] {
  return b.relation_distribution.map((d, i) => ({
    ...d,
    color: DIST_COLORS[i % DIST_COLORS.length],
  }));
}

// ─── 编译引擎设置 APIs（对象 / 关系：走真实 compiled_schema）──────

const UI_ID_SEP = '|';

function encodeUiId(prefix: string, ...parts: Array<string | null | undefined>): string {
  return [prefix, ...parts.map((part) => encodeURIComponent(part || ''))].join(UI_ID_SEP);
}

function decodeUiId(id: string, expectedPrefix: string): string[] {
  const parts = (id || '').split(UI_ID_SEP);
  if (parts[0] !== expectedPrefix) return [];
  return parts.slice(1).map((part) => decodeURIComponent(part));
}

function slugifyOntologyName(value: string): string {
  return (
    (value || '')
      .trim()
      .replace(/[\s/\\]+/g, '_')
      .replace(/[^a-zA-Z0-9_\u4e00-\u9fff]/g, '') || 'unknown'
  );
}

function ensureUniqueName(base: string, existing: Set<string>): string {
  let next = base || 'unknown';
  let idx = 2;
  while (existing.has(next)) {
    next = `${base || 'unknown'}_${idx}`;
    idx += 1;
  }
  return next;
}

function compiledAttrToOntologyType(type?: string): OntologyObjectDef['attributes'][number]['type'] {
  const mapping: Record<string, OntologyObjectDef['attributes'][number]['type']> = {
    string: '字符串',
    text: '文本',
    number: '数值',
    date: '日期',
    enum: '枚举',
    boolean: '布尔',
  };
  return mapping[(type || '').toLowerCase()] || '字符串';
}

function ontologyAttrToCompiledType(
  type?: OntologyObjectDef['attributes'][number]['type'],
): CompiledSchemaEntityType['attributes'][number]['type'] {
  const mapping: Record<string, CompiledSchemaEntityType['attributes'][number]['type']> = {
    字符串: 'string',
    文本: 'text',
    数值: 'number',
    日期: 'date',
    枚举: 'enum',
    布尔: 'boolean',
  };
  return mapping[type || ''] || 'string';
}

function compiledCardinalityToOntologyType(cardinality?: string): OntologyRelationDef['relationType'] {
  const mapping: Record<string, OntologyRelationDef['relationType']> = {
    one_to_one: '一对一',
    one_to_many: '一对多',
    many_to_one: '多对一',
    many_to_many: '多对多',
    custom: '自定义',
  };
  return mapping[(cardinality || '').toLowerCase()] || '自定义';
}

function ontologyRelationToCompiledCardinality(
  type?: OntologyRelationDef['relationType'],
): CompiledSchemaRelationType['cardinality'] {
  const mapping: Record<string, CompiledSchemaRelationType['cardinality']> = {
    一对一: 'one_to_one',
    一对多: 'one_to_many',
    多对一: 'custom',
    多对多: 'many_to_many',
    自定义: 'custom',
  };
  return mapping[type || ''] || 'custom';
}

function buildEntityDisplayMap(entityTypes: CompiledSchemaEntityType[]): Map<string, string> {
  return new Map(entityTypes.map((entity) => [entity.name, entity.display_name || entity.name]));
}

function normalizeCompiledSchemaForEditing(schema: CompiledSchema, kbId: string): CompiledSchema {
  const entityTypes = (schema.entity_types || []).map((entity) => ({
    ...entity,
    name: entity.name || slugifyOntologyName(entity.display_name || `entity_${kbId}`),
    display_name: entity.display_name || entity.name,
    description: entity.description || '',
    requirement: entity.requirement || '',
    status: entity.status || 'active',
    aliases: entity.aliases || [],
    attributes: (entity.attributes || []).map((attr) => ({
      ...attr,
      name: attr.name || slugifyOntologyName(attr.display_name || 'attr'),
      display_name: attr.display_name || attr.name,
      description: attr.description || '',
      type: ontologyAttrToCompiledType(compiledAttrToOntologyType(attr.type)),
      required: Boolean(attr.is_primary_key ?? attr.required),
      is_primary_key: Boolean(attr.is_primary_key ?? attr.required),
    })),
  }));

  const codeByDisplay = new Map<string, string>();
  entityTypes.forEach((entity) => {
    codeByDisplay.set(entity.name, entity.name);
    codeByDisplay.set(entity.display_name || entity.name, entity.name);
  });

  const relationTypes = (schema.relation_types || []).map((relation) => ({
    ...relation,
    name: relation.name || slugifyOntologyName(relation.display_name || 'relation'),
    display_name: relation.display_name || relation.name,
    description: relation.description || '',
    status: relation.status || 'active',
    aliases: relation.aliases || [],
    source: codeByDisplay.get(relation.source) || relation.source,
    target: codeByDisplay.get(relation.target) || relation.target,
    cardinality: ontologyRelationToCompiledCardinality(compiledCardinalityToOntologyType(relation.cardinality)),
  }));

  return {
    ...schema,
    entity_types: entityTypes,
    relation_types: relationTypes,
  };
}

function mapCompiledEntityToOntology(kbId: string, entity: CompiledSchemaEntityType): OntologyObjectDef {
  return {
    id: encodeUiId('obj', entity.name, entity.source_object_id || ''),
    knowledgeBaseId: kbId,
    name: entity.display_name || entity.name,
    description: entity.description || '',
    requirement: entity.requirement || '',
    status: entity.status || 'active',
    attributes: (entity.attributes || []).map((attr) => ({
      id: encodeUiId('attr', entity.name, attr.name, attr.source_attribute_id || ''),
      name: attr.display_name || attr.name,
      description: attr.description || '',
      type: compiledAttrToOntologyType(attr.type),
      isPrimaryKey: Boolean(attr.is_primary_key ?? attr.required),
    })),
  };
}

function mapCompiledRelationToOntology(
  kbId: string,
  relation: CompiledSchemaRelationType,
  entityDisplayMap: Map<string, string>,
): OntologyRelationDef {
  return {
    id: encodeUiId('rel', relation.name, relation.source, relation.target, relation.source_relation_id || ''),
    knowledgeBaseId: kbId,
    sourceObject: entityDisplayMap.get(relation.source) || relation.source,
    name: relation.display_name || relation.name,
    targetObject: entityDisplayMap.get(relation.target) || relation.target,
    description: relation.description || '',
    relationType: compiledCardinalityToOntologyType(relation.cardinality),
    status: relation.status || 'active',
  };
}

export async function fetchCompiledSchemaForEditing(kbId: string): Promise<CompiledSchema> {
  let schema: CompiledSchema | null = null;
  try {
    schema = await getData<CompiledSchema | null>(
      request.get('/knowledge-base/ontology/compiled-schema', {
        params: { knowledge_base_id: kbId },
      }),
    );
  } catch (error: any) {
    if (error?.response?.status === 405) {
      const editorState = await getOntologyEditorState(kbId);
      schema = editorState?.compiled_schema || null;
    } else {
      throw error;
    }
  }
  const fallbackSchema = schema || {
    knowledge_base_id: kbId,
    source_version: 1,
    schema_version: 1,
    entity_types: [],
    relation_types: [],
    constraints: [],
    disambiguation: {},
    prompt_schema: {},
    status: 'active',
    schema_mode: 'manual_edited',
    sync_status: 'synced',
  };
  return normalizeCompiledSchemaForEditing(fallbackSchema, kbId);
}

export async function persistCompiledOntology(
  kbId: string,
  entityTypes: CompiledSchemaEntityType[],
  relationTypes: CompiledSchemaRelationType[],
  expectedSchemaVersion: number,
  constraints?: CompiledSchemaConstraint[],
): Promise<CompiledSchema> {
  return saveOntologyCompiledSchema(kbId, entityTypes, relationTypes, expectedSchemaVersion, constraints);
}

/** 读取当前 compiled schema 中的约束数组（保底空数组）。 */
function readSchemaConstraints(schema: CompiledSchema): CompiledSchemaConstraint[] {
  return ((schema.constraints as unknown as CompiledSchemaConstraint[]) || []).filter(
    (c) => c && typeof (c as any).target_code === 'string' && (c as any).target_code,
  );
}

/** 剔除目标已不存在的约束（对象/属性/关系删除或改名后自愈）。 */
function pruneDanglingConstraints(
  constraints: CompiledSchemaConstraint[],
  entityTypes: CompiledSchemaEntityType[],
  relationTypes: CompiledSchemaRelationType[],
): CompiledSchemaConstraint[] {
  const entityCodes = new Set(entityTypes.map((e) => e.name));
  const attrCodes = new Set(entityTypes.flatMap((e) => (e.attributes || []).map((a) => `${e.name}.${a.name}`)));
  const relationCodes = new Set(relationTypes.map((r) => r.name));
  return constraints.filter((c) => {
    if (c.target_type === 'entity') return entityCodes.has(c.target_code);
    if (c.target_type === 'attribute') return attrCodes.has(c.target_code);
    if (c.target_type === 'relation') return relationCodes.has(c.target_code);
    return false;
  });
}

function mapCompiledConstraintToUi(c: CompiledSchemaConstraint): OntologyConstraint | null {
  if (!c || !c.target_code) return null;
  return {
    id: encodeUiId('constraint', c.name),
    name: c.name,
    targetType: c.target_type,
    targetCode: c.target_code,
    targetLabel: c.target_label ?? undefined,
    constraintType: c.constraint_type || 'custom',
    expression: c.expression || '',
    suggestion: c.suggestion || '',
  };
}

function buildCompiledConstraintFromPayload(p: SaveOntologyConstraintPayload): CompiledSchemaConstraint {
  return {
    name: (p.name || '').trim(),
    target_type: p.targetType,
    target_code: p.targetCode,
    target_label: p.targetLabel || null,
    constraint_type: (p.constraintType || 'custom').trim(),
    expression: (p.expression || '').trim(),
    suggestion: (p.suggestion || '').trim(),
  };
}

function decodeEntityMeta(id: string): {
  compiledName: string;
  sourceObjectId?: string | null;
} {
  const [compiledName, sourceObjectId] = decodeUiId(id, 'obj');
  return {
    compiledName: compiledName || '',
    sourceObjectId: sourceObjectId || null,
  };
}

function decodeAttrMeta(id?: string): {
  compiledName: string;
  sourceAttributeId?: string | null;
} {
  const [, compiledName, sourceAttributeId] = decodeUiId(id || '', 'attr');
  return {
    compiledName: compiledName || '',
    sourceAttributeId: sourceAttributeId || null,
  };
}

function decodeRelationMeta(id: string): {
  compiledName: string;
  source: string;
  target: string;
  sourceRelationId?: string | null;
} {
  const [compiledName, source, target, sourceRelationId] = decodeUiId(id, 'rel');
  return {
    compiledName: compiledName || '',
    source: source || '',
    target: target || '',
    sourceRelationId: sourceRelationId || null,
  };
}

function buildCompiledEntityFromPayload(
  payload: SaveOntologyObjectPayload,
  existingNames: Set<string>,
  existing?: CompiledSchemaEntityType,
): CompiledSchemaEntityType {
  const compiledName = existing?.name || ensureUniqueName(slugifyOntologyName(payload.name), existingNames);
  const attrNames = new Set<string>();
  return {
    name: compiledName,
    display_name: payload.name,
    description: payload.description || '',
    requirement: payload.requirement || '',
    status: payload.status || 'active',
    aliases: existing?.aliases || [],
    source_object_id: existing?.source_object_id || null,
    attributes: (payload.attributes || []).map((attr) => {
      const attrMeta = decodeAttrMeta(attr.id);
      const attrName = attrMeta.compiledName || ensureUniqueName(slugifyOntologyName(attr.name), attrNames);
      attrNames.add(attrName);
      return {
        name: attrName,
        display_name: attr.name,
        description: attr.description || '',
        type: ontologyAttrToCompiledType(attr.type),
        required: Boolean(attr.isPrimaryKey),
        is_primary_key: Boolean(attr.isPrimaryKey),
        source_attribute_id: attrMeta.sourceAttributeId || null,
      };
    }),
  };
}

function buildCompiledRelationFromPayload(
  payload: SaveOntologyRelationPayload,
  entityTypes: CompiledSchemaEntityType[],
  existingNames: Set<string>,
  existing?: CompiledSchemaRelationType,
): CompiledSchemaRelationType {
  const entityCodeByDisplay = new Map<string, string>();
  entityTypes.forEach((entity) => {
    entityCodeByDisplay.set(entity.display_name || entity.name, entity.name);
    entityCodeByDisplay.set(entity.name, entity.name);
  });

  const sourceCode = entityCodeByDisplay.get(payload.sourceObject);
  const targetCode = entityCodeByDisplay.get(payload.targetObject);
  if (!sourceCode || !targetCode) {
    throw new Error('Referenced object does not exist. Please create the object before saving the relation.');
  }

  return {
    name: existing?.name || ensureUniqueName(slugifyOntologyName(payload.name), existingNames),
    display_name: payload.name,
    description: payload.description || '',
    status: payload.status || 'active',
    aliases: existing?.aliases || [],
    source: sourceCode,
    target: targetCode,
    source_relation_id: existing?.source_relation_id || null,
    cardinality: ontologyRelationToCompiledCardinality(payload.relationType),
  };
}

export async function getOntologyObjects(kbId: string): Promise<OntologyObjectDef[]> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  return (schema.entity_types || []).map((entity) => mapCompiledEntityToOntology(kbId, entity));
}

export async function createOntologyObject(kbId: string, p: SaveOntologyObjectPayload): Promise<OntologyObjectDef> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const entityTypes = schema.entity_types || [];
  const nextEntity = buildCompiledEntityFromPayload(p, new Set(entityTypes.map((entity) => entity.name)));
  // 新增对象不会使既有约束悬挂，constraints 传 undefined=后端保留
  await persistCompiledOntology(kbId, [...entityTypes, nextEntity], schema.relation_types || [], schema.schema_version);
  return mapCompiledEntityToOntology(kbId, nextEntity);
}

export async function updateOntologyObject(
  kbId: string,
  id: string,
  p: SaveOntologyObjectPayload,
): Promise<OntologyObjectDef> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const entityTypes = schema.entity_types || [];
  const relationTypes = schema.relation_types || [];
  const meta = decodeEntityMeta(id);
  const current = entityTypes.find((entity) => entity.name === meta.compiledName);
  if (!current) throw new Error('Object does not exist');

  const nextEntity = buildCompiledEntityFromPayload(p, new Set(entityTypes.map((entity) => entity.name)), current);
  const nextEntities = entityTypes.map((entity) => (entity.name === current.name ? nextEntity : entity));
  // 属性可能被删除，剔除悬挂约束（针对该实体属性目标）后显式回写
  const nextConstraints = pruneDanglingConstraints(readSchemaConstraints(schema), nextEntities, relationTypes);
  await persistCompiledOntology(kbId, nextEntities, relationTypes, schema.schema_version, nextConstraints);
  return mapCompiledEntityToOntology(kbId, nextEntity);
}

export async function deleteOntologyObject(kbId: string, id: string): Promise<boolean> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const meta = decodeEntityMeta(id);
  const nextEntities = (schema.entity_types || []).filter((entity) => entity.name !== meta.compiledName);
  const nextRelations = (schema.relation_types || []).filter(
    (relation) => relation.source !== meta.compiledName && relation.target !== meta.compiledName,
  );
  // 级联清理：删除对象连带其实体/属性/相关关系的约束
  const nextConstraints = pruneDanglingConstraints(readSchemaConstraints(schema), nextEntities, nextRelations);
  await persistCompiledOntology(kbId, nextEntities, nextRelations, schema.schema_version, nextConstraints);
  return true;
}

export async function getOntologyRelations(kbId: string): Promise<OntologyRelationDef[]> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const entityDisplayMap = buildEntityDisplayMap(schema.entity_types || []);
  return (schema.relation_types || []).map((relation) =>
    mapCompiledRelationToOntology(kbId, relation, entityDisplayMap),
  );
}

/** @deprecated 使用 createSchemaRelationType */
export async function createSchemaRelationType(
  kbId: string,
  p: SaveOntologyRelationPayload,
): Promise<OntologyRelationDef> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const relationTypes = schema.relation_types || [];
  const nextRelation = buildCompiledRelationFromPayload(
    p,
    schema.entity_types || [],
    new Set(relationTypes.map((relation) => relation.name)),
  );
  const nextRelations = [...relationTypes, nextRelation];
  await persistCompiledOntology(kbId, schema.entity_types || [], nextRelations, schema.schema_version);
  const entityDisplayMap = buildEntityDisplayMap(schema.entity_types || []);
  return mapCompiledRelationToOntology(kbId, nextRelation, entityDisplayMap);
}

export async function updateSchemaRelationType(
  kbId: string,
  id: string,
  p: SaveOntologyRelationPayload,
): Promise<OntologyRelationDef> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const entityTypes = schema.entity_types || [];
  const relationTypes = schema.relation_types || [];
  const meta = decodeRelationMeta(id);
  const current = relationTypes.find(
    (relation) =>
      relation.name === meta.compiledName && relation.source === meta.source && relation.target === meta.target,
  );
  if (!current) throw new Error('Relation does not exist');

  const nextRelation = buildCompiledRelationFromPayload(
    p,
    entityTypes,
    new Set(relationTypes.map((relation) => relation.name)),
    current,
  );
  const nextRelations = relationTypes.map((relation) =>
    relation.name === current.name && relation.source === current.source && relation.target === current.target
      ? nextRelation
      : relation,
  );
  await persistCompiledOntology(kbId, entityTypes, nextRelations, schema.schema_version);
  return mapCompiledRelationToOntology(kbId, nextRelation, buildEntityDisplayMap(entityTypes));
}

export async function deleteSchemaRelationType(kbId: string, id: string): Promise<boolean> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const meta = decodeRelationMeta(id);
  const nextRelations = (schema.relation_types || []).filter(
    (relation) =>
      !(relation.name === meta.compiledName && relation.source === meta.source && relation.target === meta.target),
  );
  // 级联清理：删除关系连带其关系约束
  const nextConstraints = pruneDanglingConstraints(
    readSchemaConstraints(schema),
    schema.entity_types || [],
    nextRelations,
  );
  await persistCompiledOntology(kbId, schema.entity_types || [], nextRelations, schema.schema_version, nextConstraints);
  return true;
}

// ─── 本体约束定义（读改 compiled_schema.constraints → 全量保存）──────

/** 供 ConstraintFormModal 目标对象联动选择的 code/label 选项。 */
export interface ConstraintTargetOption {
  value: string;
  label: string;
}
export interface ConstraintTargetOptions {
  entity: ConstraintTargetOption[];
  attribute: ConstraintTargetOption[];
  relation: ConstraintTargetOption[];
}

export async function getConstraintTargetOptions(kbId: string): Promise<ConstraintTargetOptions> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const entity = (schema.entity_types || []).map((e) => ({
    value: e.name,
    label: e.display_name || e.name,
  }));
  const attribute = (schema.entity_types || []).flatMap((e) =>
    (e.attributes || []).map((a) => ({
      value: `${e.name}.${a.name}`,
      label: `${e.display_name || e.name}.${a.display_name || a.name}`,
    })),
  );
  const relation = (schema.relation_types || []).map((r) => ({
    value: r.name,
    label: r.display_name || r.name,
  }));
  return { entity, attribute, relation };
}

export async function getOntologyConstraints(kbId: string): Promise<OntologyConstraint[]> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  return readSchemaConstraints(schema)
    .map(mapCompiledConstraintToUi)
    .filter((c): c is OntologyConstraint => c !== null);
}

export async function createOntologyConstraint(
  kbId: string,
  p: SaveOntologyConstraintPayload,
): Promise<OntologyConstraint> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const constraints = readSchemaConstraints(schema);
  const name = (p.name || '').trim();
  if (constraints.some((c) => c.name === name)) {
    throw new Error(`Constraint name already exists: ${name}`);
  }
  const next = buildCompiledConstraintFromPayload({ ...p, name });
  await persistCompiledOntology(kbId, schema.entity_types || [], schema.relation_types || [], schema.schema_version, [
    ...constraints,
    next,
  ]);
  return mapCompiledConstraintToUi(next) as OntologyConstraint;
}

export async function updateOntologyConstraint(
  kbId: string,
  id: string,
  p: SaveOntologyConstraintPayload,
): Promise<OntologyConstraint> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const constraints = readSchemaConstraints(schema);
  const [originalName] = decodeUiId(id, 'constraint');
  const current = constraints.find((c) => c.name === originalName);
  if (!current) throw new Error('Constraint does not exist');
  const nextName = (p.name || '').trim();
  if (nextName !== originalName && constraints.some((c) => c.name === nextName)) {
    throw new Error(`Constraint name already exists: ${nextName}`);
  }
  const next = buildCompiledConstraintFromPayload({ ...p, name: nextName });
  const nextConstraints = constraints.map((c) => (c.name === originalName ? next : c));
  await persistCompiledOntology(
    kbId,
    schema.entity_types || [],
    schema.relation_types || [],
    schema.schema_version,
    nextConstraints,
  );
  return mapCompiledConstraintToUi(next) as OntologyConstraint;
}

export async function deleteOntologyConstraint(kbId: string, id: string): Promise<boolean> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const constraints = readSchemaConstraints(schema);
  const [originalName] = decodeUiId(id, 'constraint');
  const nextConstraints = constraints.filter((c) => c.name !== originalName);
  await persistCompiledOntology(
    kbId,
    schema.entity_types || [],
    schema.relation_types || [],
    schema.schema_version,
    nextConstraints,
  );
  return true;
}

export async function getCompileSteps(_kbId: string): Promise<CompileStep[]> {
  // TODO: 替换为真实后端接口
  return [];
}
export async function createCompileStep(_kbId: string, _p: SaveCompileStepPayload): Promise<CompileStep> {
  // TODO: 替换为真实后端接口
  throw new Error('Not implemented');
}
export async function updateCompileStep(_kbId: string, _id: string, _p: SaveCompileStepPayload): Promise<CompileStep> {
  // TODO: 替换为真实后端接口
  throw new Error('Not implemented');
}
export async function deleteCompileStep(_kbId: string, _id: string): Promise<boolean> {
  // TODO: 替换为真实后端接口
  throw new Error('Not implemented');
}
export async function getEngineSetting(kbId: string): Promise<EngineSetting> {
  // TODO: 替换为真实后端接口
  return { knowledgeBaseId: kbId, semanticModel: 'claude-sonnet-4-6' };
}
export async function saveEngineSetting(kbId: string, semanticModel: string): Promise<EngineSetting> {
  // TODO: 替换为真实后端接口
  return { knowledgeBaseId: kbId, semanticModel };
}

/** 获取全局解析器配置列表（business_domain 全局 parser_configs） */
export interface ParserConfigItem {
  id: string;
  name: string;
  parser_type: string;
  file_types: string[];
  config_json: Record<string, any>;
  status: string;
  created_at: string;
  updated_at: string;
}

export async function getParserConfigs(offset = 0, limit = 100): Promise<{ items: ParserConfigItem[]; total: number }> {
  return getData<{ items: ParserConfigItem[]; total: number }>(
    request.get('/ecosystem/parser-configs', { params: { offset, limit } }),
  );
}

export interface ParserSettingItem {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  parser_type: string;
  parser_config_id?: string | null;
  parser_name?: string;
  parser_file_types?: string[];
  parser_status?: string | null;
  preprocessing_json: string[];
  postprocessing_json: string[];
  prompt_text?: string;
  prompt_template_id?: string | null;
  prompt_template_version?: string | null;
  summary_prompt_text?: string;
  summary_template_id?: string | null;
  summary_template_version?: string | null;
  tag_prompt_text?: string;
  tag_template_id?: string | null;
  tag_template_version?: string | null;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ParserSettingPayload {
  knowledge_base_id: string;
  parser_type: string;
  parser_config_id?: string | null;
  preprocessing_json?: string[];
  postprocessing_json?: string[];
  prompt_text?: string;
  prompt_template_id?: string | null;
  prompt_template_version?: string | null;
  summary_prompt_text?: string;
  summary_template_id?: string | null;
  summary_template_version?: string | null;
  tag_prompt_text?: string;
  tag_template_id?: string | null;
  tag_template_version?: string | null;
  status?: string;
}

export async function getParserSettings(
  knowledgeBaseId: string,
): Promise<{ items: ParserSettingItem[]; total: number }> {
  return getData<{ items: ParserSettingItem[]; total: number }>(
    request.get('/knowledge-base/parser-settings', {
      params: { knowledge_base_id: knowledgeBaseId },
    }),
  );
}

export async function createParserSetting(payload: ParserSettingPayload): Promise<ParserSettingItem> {
  return postData<ParserSettingItem>('/knowledge-base/parser-settings', payload);
}

export async function updateParserSetting(
  settingId: string,
  payload: Partial<ParserSettingPayload>,
): Promise<ParserSettingItem> {
  return getData<ParserSettingItem>(request.patch(`/knowledge-base/parser-settings/${settingId}`, payload));
}

export async function deleteParserSetting(settingId: string): Promise<{ deleted: boolean; id: string }> {
  return deleteData<{ deleted: boolean; id: string }>(`/knowledge-base/parser-settings/${settingId}`);
}

export interface PromptTemplateVersionItem {
  version: string;
  content: string;
  updated_by?: string;
  updated_at?: string;
  remark?: string;
}

export interface PromptTemplateListItem {
  id: string;
  tenant_id: string | null;
  name: string;
  category: string;
  scope: 'system' | 'domain';
  description?: string;
  status: string;
  current_version: string;
  versions_json: PromptTemplateVersionItem[];
  space_id?: string | null;
  created_by?: string;
  created_at: string | null;
  updated_at: string | null;
}

export async function listPromptTemplates(
  params: {
    scope?: string;
    category?: string;
    keyword?: string;
    domain_space_id?: string;
    offset?: number;
    limit?: number;
  } = {},
): Promise<{
  items: PromptTemplateListItem[];
  total: number;
  offset: number;
  limit: number;
}> {
  return getData<{
    items: PromptTemplateListItem[];
    total: number;
    offset: number;
    limit: number;
  }>(request.get('/ecosystem/prompt-templates', { params }));
}

export async function importOntologyObjectsFromTemplate(
  kbId: string,
  items: SaveOntologyObjectPayload[],
): Promise<{ created: OntologyObjectDef[]; skipped: number }> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const entityTypes = [...(schema.entity_types || [])];
  const existingDisplayNames = new Set(entityTypes.map((entity) => entity.display_name || entity.name));
  const existingCodes = new Set(entityTypes.map((entity) => entity.name));
  const createdCompiled: CompiledSchemaEntityType[] = [];
  let skipped = 0;

  for (const item of items) {
    if (existingDisplayNames.has(item.name)) {
      skipped += 1;
      continue;
    }
    const nextEntity = buildCompiledEntityFromPayload(item, existingCodes);
    existingDisplayNames.add(item.name);
    existingCodes.add(nextEntity.name);
    entityTypes.push(nextEntity);
    createdCompiled.push(nextEntity);
  }

  if (createdCompiled.length > 0) {
    await persistCompiledOntology(kbId, entityTypes, schema.relation_types || [], schema.schema_version);
  }

  return {
    created: createdCompiled.map((entity) => mapCompiledEntityToOntology(kbId, entity)),
    skipped,
  };
}

export async function importOntologyRelationsFromTemplate(
  kbId: string,
  items: SaveOntologyRelationPayload[],
): Promise<{ created: OntologyRelationDef[]; skipped: number }> {
  const schema = await fetchCompiledSchemaForEditing(kbId);
  const entityTypes = schema.entity_types || [];
  const relationTypes = [...(schema.relation_types || [])];
  const existingKeys = new Set(
    relationTypes.map((relation) => `${relation.source}|${relation.display_name || relation.name}|${relation.target}`),
  );
  const existingNames = new Set(relationTypes.map((relation) => relation.name));
  const createdCompiled: CompiledSchemaRelationType[] = [];
  let skipped = 0;

  for (const item of items) {
    const entityCodeByDisplay = new Map<string, string>();
    entityTypes.forEach((entity) => {
      entityCodeByDisplay.set(entity.display_name || entity.name, entity.name);
      entityCodeByDisplay.set(entity.name, entity.name);
    });
    const sourceCode = entityCodeByDisplay.get(item.sourceObject);
    const targetCode = entityCodeByDisplay.get(item.targetObject);
    if (!sourceCode || !targetCode) {
      skipped += 1;
      continue;
    }
    const key = `${sourceCode}|${item.name}|${targetCode}`;
    if (existingKeys.has(key)) {
      skipped += 1;
      continue;
    }
    const nextRelation = buildCompiledRelationFromPayload(item, entityTypes, existingNames);
    existingKeys.add(`${nextRelation.source}|${nextRelation.display_name || nextRelation.name}|${nextRelation.target}`);
    existingNames.add(nextRelation.name);
    relationTypes.push(nextRelation);
    createdCompiled.push(nextRelation);
  }

  if (createdCompiled.length > 0) {
    await persistCompiledOntology(kbId, entityTypes, relationTypes, schema.schema_version);
  }

  const entityDisplayMap = buildEntityDisplayMap(entityTypes);
  return {
    created: createdCompiled.map((relation) => mapCompiledRelationToOntology(kbId, relation, entityDisplayMap)),
    skipped,
  };
}

// ═══════════════════════════════════════════════════════════
// ─── 领域知识结果 — 真实后端 API（ontology_query_service）─
// ═══════════════════════════════════════════════════════════

/** 获取 kb 维度统计：源文件数 + 本体实例/关系数 */
export function getOntologyStatistics(kbId: string): Promise<OntologyStatistics> {
  return getData<OntologyStatistics>(
    request.get('/knowledge-base/ontology/statistics', {
      params: { knowledge_base_id: kbId },
    }),
  );
}

/** 获取实体类型列表（含实例数聚合） */
export function getOntologyEntityTypes(
  kbId: string,
  keyword?: string,
): Promise<{ items: OntologyInstanceSummary[]; total: number }> {
  const params: Record<string, string> = { knowledge_base_id: kbId };
  if (keyword) params.keyword = keyword;
  return getData<{ items: OntologyInstanceSummary[]; total: number }>(
    request.get('/knowledge-base/ontology/entity-types', {
      params,
    }),
  );
}

/** 获取关系类型列表（含实例数聚合） */
export function getOntologyRelationTypes(kbId: string): Promise<{ items: RelationInstanceSummary[]; total: number }> {
  return getData<{ items: RelationInstanceSummary[]; total: number }>(
    request.get('/knowledge-base/ontology/relation-types', {
      params: { knowledge_base_id: kbId },
    }),
  );
}

/** 分页查询本体实例列表 */
export function getOntologyInstances(params: OntologyInstanceListParams): Promise<{
  items: OntologyInstanceRow[];
  total: number;
  page: number;
  page_size: number;
}> {
  const query: Record<string, string | number | undefined> = {
    knowledge_base_id: params.kbId,
    page: params.page,
    page_size: params.pageSize,
    entity_type: params.entityType,
    keyword: params.keyword,
    document_id: params.docId,
  };
  const cleanQuery: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== '') cleanQuery[k] = v;
  }
  return getData<{
    items: OntologyInstanceRow[];
    total: number;
    page: number;
    page_size: number;
  }>(request.get('/knowledge-base/ontology/instances', { params: cleanQuery }));
}

/** 按 chunk_id 精确查单片 chunk 完整内容（GET /knowledge-base/documents/{doc_id}/chunks/{chunk_id}） */
export function getChunkDetail(
  documentId: string,
  chunkId: string,
): Promise<{ doc_id: string; chunk_id: string; content: string; chunk_index: number }> {
  return getData(request.get(`/knowledge-base/documents/${documentId}/chunks/${chunkId}`));
}

/** 搜索本体实例（GET /knowledge-base/ontology/entities/search） */
export function searchOntologyInstances(
  kbId: string,
  keyword?: string,
  limit = 20,
): Promise<{
  items: OntologyInstanceRow[];
  total: number;
  page: number;
  page_size: number;
}> {
  const params: Record<string, string> = { knowledge_base_id: kbId };
  if (keyword) params.keyword = keyword;
  params.limit = String(limit);
  return getData<{
    items: OntologyInstanceRow[];
    total: number;
    page: number;
    page_size: number;
  }>(request.get('/knowledge-base/ontology/entities/search', { params }));
}

/** 分页查询本体关系列表 */
export function getOntologyRelationInstances(params: RelationInstanceListParams): Promise<{
  items: RelationInstanceRow[];
  total: number;
  page: number;
  page_size: number;
}> {
  const query: Record<string, string | number | undefined> = {
    knowledge_base_id: params.kbId,
    page: params.page,
    page_size: params.pageSize,
    keyword: params.keyword,
    relation_type: params.relationType,
    source_name: params.sourceName,
    target_name: params.targetName,
    source_type: params.sourceType,
    target_type: params.targetType,
    document_id: params.docId,
  };
  const cleanQuery: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== '') cleanQuery[k] = v;
  }
  return getData<{
    items: RelationInstanceRow[];
    total: number;
    page: number;
    page_size: number;
  }>(request.get('/knowledge-base/ontology/relations', { params: cleanQuery }));
}

/** 获取 KB 图谱数据（nodes + edges + 统计），支持类型筛选与节点上限 */
export function getOntologyGraph(kbId: string, params?: OntologyGraphParams): Promise<OntologyGraphData> {
  return getData<OntologyGraphData>(
    request.get('/knowledge-base/ontology/graph', {
      params: {
        knowledge_base_id: kbId,
        limit: params?.limit,
        entity_types: params?.entityTypes,
      },
      // 数组参数序列化为 entity_types=A&entity_types=B（FastAPI list[str] 期望格式）
      paramsSerializer: { indexes: null },
    }),
  );
}

/** 展开某实体的一跳邻居，返回邻居节点与连接边（增量并入当前图） */
export function expandOntologyNeighbors(
  kbId: string,
  entityType: string,
  canonicalName: string,
  limit = 50,
): Promise<OntologyNeighborData> {
  return getData<OntologyNeighborData>(
    request.get('/knowledge-base/ontology/neighbors', {
      params: {
        knowledge_base_id: kbId,
        entity_type: entityType,
        canonical_name: canonicalName,
        limit,
      },
    }),
  );
}

// ═══════════════════════════════════════════════════════════
// 本体实例/关系 CRUD（Neo4j 层，与 compiled_schema 的 schema 定义操作区分）
// ═══════════════════════════════════════════════════════════

/** 创建本体实例（Neo4j 实体节点） */
export async function createOntologyInstance(
  data: CreateOntologyInstanceRequest,
): Promise<CreateOntologyInstanceResponse> {
  return postData<CreateOntologyInstanceResponse>('/knowledge-base/ontology/instances', data);
}

/** 更新本体实例 */
export async function updateOntologyInstance(
  knowledgeBaseId: string,
  entityType: string,
  canonicalName: string,
  updates: Record<string, unknown>,
): Promise<UpdateOntologyInstanceResponse> {
  return putData<UpdateOntologyInstanceResponse>(
    `/knowledge-base/ontology/instances/${entityType}/${encodeURIComponent(canonicalName)}?knowledge_base_id=${knowledgeBaseId}`,
    { updates },
  );
}

/** 删除本体实例 */
export async function deleteOntologyInstance(
  knowledgeBaseId: string,
  entityType: string,
  canonicalName: string,
): Promise<DeleteOntologyInstanceResponse> {
  return deleteData<DeleteOntologyInstanceResponse>(
    `/knowledge-base/ontology/instances/${entityType}/${encodeURIComponent(canonicalName)}?knowledge_base_id=${knowledgeBaseId}`,
  );
}

/** 创建本体关系（Neo4j 关系边） */
export async function createOntologyRelation(
  knowledgeBaseId: string,
  sourceEntityType: string,
  sourceCanonicalName: string,
  relationType: string,
  targetEntityType: string,
  targetCanonicalName: string,
  attributes?: Record<string, unknown>,
): Promise<CreateOntologyRelationResponse> {
  const params = new URLSearchParams({
    knowledge_base_id: knowledgeBaseId,
    source_entity_type: sourceEntityType,
    source_canonical_name: sourceCanonicalName,
    relation_type: relationType,
    target_entity_type: targetEntityType,
    target_canonical_name: targetCanonicalName,
  });
  return postData<CreateOntologyRelationResponse>(
    `/knowledge-base/ontology/relations?${params.toString()}`,
    attributes && Object.keys(attributes).length > 0 ? { attributes } : undefined,
  );
}

/** 更新本体关系 */
export async function updateOntologyRelation(
  knowledgeBaseId: string,
  sourceEntityType: string,
  sourceCanonicalName: string,
  relationType: string,
  targetEntityType: string,
  targetCanonicalName: string,
  updates: { relation_type?: string; attributes?: Record<string, unknown> },
): Promise<UpdateOntologyRelationResponse> {
  const params = new URLSearchParams({
    knowledge_base_id: knowledgeBaseId,
    source_entity_type: sourceEntityType,
    source_canonical_name: sourceCanonicalName,
    relation_type: relationType,
    target_entity_type: targetEntityType,
    target_canonical_name: targetCanonicalName,
  });
  return putData<UpdateOntologyRelationResponse>(`/knowledge-base/ontology/relations?${params.toString()}`, {
    updates,
  });
}

/** 删除本体关系 */
export async function deleteOntologyRelation(
  knowledgeBaseId: string,
  sourceEntityType: string,
  sourceCanonicalName: string,
  relationType: string,
  targetEntityType: string,
  targetCanonicalName: string,
): Promise<DeleteOntologyRelationResponse> {
  const params = new URLSearchParams({
    knowledge_base_id: knowledgeBaseId,
    source_entity_type: sourceEntityType,
    source_canonical_name: sourceCanonicalName,
    relation_type: relationType,
    target_entity_type: targetEntityType,
    target_canonical_name: targetCanonicalName,
  });
  return deleteData<DeleteOntologyRelationResponse>(`/knowledge-base/ontology/relations?${params.toString()}`);
}

// ═══════════════════════════════════════════════════════════
// 文件夹
export function getFolderList(kbId: string): Promise<FolderListResponse> {
  return getData<FolderListResponse>(
    request.get('/knowledge-base/folders', {
      params: {
        knowledge_base_id: kbId,
      },
    }),
  );
}

export function createFolder(kbId: string, name: string): Promise<FolderItem> {
  return postData<FolderItem>('/knowledge-base/folders', {
    knowledge_base_id: kbId,
    name,
  });
}

export function renameFolder(folderId: string, kbId: string, name: string): Promise<FolderItem> {
  return getData<FolderItem>(
    request.patch(
      `/knowledge-base/folders/${folderId}`,
      { name },
      {
        params: { knowledge_base_id: kbId },
      },
    ),
  );
}

export async function deleteFolder(folderId: string, kbId: string): Promise<void> {
  await getData(
    request.delete(`/knowledge-base/folders/${folderId}`, {
      params: { knowledge_base_id: kbId },
    }),
  );
}

export interface BatchMoveResult {
  moved_count: number;
  skipped_count: number;
  folder_id: string | null;
}

/** 批量移动文档到指定目录；folderId 传 null 表示移出到未分类。 */
export function batchMoveDocuments(
  knowledgeBaseId: string,
  documentIds: string[],
  folderId: string | null,
): Promise<BatchMoveResult> {
  return putData<BatchMoveResult>('/knowledge-base/documents/folder', {
    knowledge_base_id: knowledgeBaseId,
    document_ids: documentIds,
    folder_id: folderId,
  });
}

// ═══════════════════════════════════════════════════════════
// 标签
export interface TagItem {
  id: string;
  name: string;
  color: string;
}

export function getKnowledgeBaseTags(kbId: string): Promise<TagItem[]> {
  return getData<{ items: TagItem[] }>(
    request.get('/knowledge-base/tags', {
      params: { knowledge_base_id: kbId },
    }),
  ).then((res) => res.items);
}

export function createKnowledgeBaseTag(data: {
  knowledge_base_id: string;
  name: string;
  color: string;
}): Promise<TagItem> {
  return postData<TagItem>('/knowledge-base/tags', data);
}

export function getDocumentTags(documentId: string, kbId: string): Promise<TagItem[]> {
  return getData<{ items: TagItem[] }>(
    request.get(`/knowledge-base/documents/${documentId}/tags`, {
      params: { knowledge_base_id: kbId },
    }),
  ).then((res) => res.items);
}

export function setDocumentTags(
  documentId: string,
  data: { knowledge_base_id: string; tag_ids: string[] },
): Promise<void> {
  return putData(`/knowledge-base/documents/${documentId}/tags`, data);
}

// ─── YAML 导入/导出 APIs ────────────────────────────────

export async function exportCompiledSchemaYaml(
  kbId: string,
): Promise<{ filename: string; yaml_text: string; warnings: string[] }> {
  return getData<{ filename: string; yaml_text: string; warnings: string[] }>(
    request.get('/knowledge-base/ontology/compiled-schema/yaml', {
      params: { knowledge_base_id: kbId },
    }),
  );
}

export async function importCompiledSchemaYaml(
  kbId: string,
  expectedSchemaVersion: number,
  file: File,
  dryRun: boolean,
): Promise<YamlImportResult> {
  const fd = new FormData();
  fd.append('knowledge_base_id', kbId);
  fd.append('expected_schema_version', String(expectedSchemaVersion));
  fd.append('file', file);
  if (dryRun) {
    fd.append('dry_run', 'true');
  }
  return getData<YamlImportResult>(
    request.post('/knowledge-base/ontology/compiled-schema/yaml/import', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),
  );
}

// ── [jonex] OpenKB Wiki 只读视图取数（复用 parse-results/* 路由）──

interface BackendWikiEntityItem {
  id: string;
  name: string;
  type: string;
  description: string;
  relations_count?: number;
}

interface BackendWikiRelationshipItem {
  id: string;
  source_entity: string;
  target_entity: string;
  description: string;
}

interface BackendWikiPage<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** Wiki 实体/概念列表（openkb 管线专用） */
export function getWikiEntities(params: {
  kbId: string;
  page: number;
  pageSize: number;
  keyword?: string;
  entityType?: string;
}): Promise<PaginationResult<WikiEntityRow>> {
  const query: Record<string, string | number> = {
    knowledge_base_id: params.kbId,
    page: params.page,
    page_size: params.pageSize,
  };
  if (params.keyword) query.keyword = params.keyword;
  if (params.entityType) query.entity_type = params.entityType;

  return getData<BackendWikiPage<BackendWikiEntityItem>>(
    request.get('/knowledge-base/parse-results/entities', { params: query }),
  ).then((res) => ({
    list: (res.items || []).map((it) => ({
      id: it.id || it.name,
      name: it.name || '',
      type: it.type || '',
      description: it.description || '',
      relationsCount: it.relations_count ?? 0,
    })),
    pagination: { page: res.page, pageSize: res.page_size, total: res.total },
  }));
}

/** Wiki 关系列表（openkb 管线专用） */
export function getWikiRelationships(params: {
  kbId: string;
  page: number;
  pageSize: number;
  keyword?: string;
}): Promise<PaginationResult<WikiRelationshipRow>> {
  const query: Record<string, string | number> = {
    knowledge_base_id: params.kbId,
    page: params.page,
    page_size: params.pageSize,
  };
  if (params.keyword) query.keyword = params.keyword;

  return getData<BackendWikiPage<BackendWikiRelationshipItem>>(
    request.get('/knowledge-base/parse-results/relationships', { params: query }),
  ).then((res) => ({
    list: (res.items || []).map((it) => ({
      id: it.id || `${it.source_entity}->${it.target_entity}`,
      source: it.source_entity || '',
      target: it.target_entity || '',
      description: it.description || '',
    })),
    pagination: { page: res.page, pageSize: res.page_size, total: res.total },
  }));
}

/** Wiki 图谱（openkb 管线专用） */
export function getWikiGraph(kbId: string, limit = 200, documentId?: string): Promise<WikiGraphData> {
  // [jonex] documentId 缺省/空 = KB 级全库图谱（compile-results 页）；省略参数避免空串语义
  const params: Record<string, unknown> = { knowledge_base_id: kbId, limit };
  if (documentId) params.document_id = documentId;
  return getData<{
    nodes?: { id: string; name: string; type: string; description: string }[];
    edges?: { id: string; source: string; target: string }[];
  }>(request.get('/knowledge-base/parse-results/graph', { params })).then((res) => ({
    nodes: res.nodes || [],
    edges: res.edges || [],
  }));
}

// ── [jonex] Wiki 阅读模式（编译结果页）──

/** 读取 Wiki 页面内容（openkb 管线专用；后端字段本已是 camelCase 结构，直接透传） */
export function getWikiPage(kbId: string, path: string): Promise<WikiPageContent> {
  return getData<WikiPageContent>(
    request.get('/knowledge-base/parse-results/wiki-page', {
      params: { knowledge_base_id: kbId, path },
    }),
  );
}

/** 后端原始结构（snake_case，不入组件） */
interface BackendWikiContents {
  summaries: WikiPageItem[];
  concepts: WikiPageItem[];
  entities: WikiPageItem[];
  document_id: string;
}

/** Wiki 页面树（openkb 管线专用；documentId 缺省/空 = KB 级全库，compile-results 页用） */
export function getWikiContents(kbId: string, documentId?: string): Promise<WikiContents> {
  // [jonex] documentId 空时省略参数（Gateway 缺省 = KB 级全库）
  const params: Record<string, unknown> = { knowledge_base_id: kbId };
  if (documentId) params.document_id = documentId;
  return getData<BackendWikiContents>(
    request.get('/knowledge-base/parse-results/wiki-contents', { params }),
  ).then((res) => ({
    summaries: res.summaries || [],
    concepts: res.concepts || [],
    entities: res.entities || [],
    documentId: res.document_id || '',
  }));
}
