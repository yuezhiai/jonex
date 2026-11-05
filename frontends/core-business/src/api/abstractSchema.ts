/**
 * 跨数据源抽象 schema（v2：完整 schema = 实体 + 关系）客户端。
 *
 * 形状与编译 schema 对齐（可被投影成它）：实体挂 N 个源落点（`included` 控纳入），
 * 关系挂物理背书（backings，可空 = 逻辑关系）。响应 snake_case → camelCase 在本地
 * `mapX` 里做（本仓没有 codegen）；`stale`/`staleReason` 是**读时算**的派生字段。
 */
import { getData, request } from './request';
import type { ApiResponse } from './request';

export interface AbstractLanding {
  dataSourceId: string;
  entityName: string;
  /** 是否纳入 schema（false = 对齐保留、不生效：投影/检索/DI 都不含它）。 */
  included: boolean;
  sourceName?: string | null;
  stale: boolean;
  staleReason?: 'source_gone' | 'entity_gone' | 'attribute_gone' | 'relation_gone' | null;
}

export interface AbstractAttributeSource {
  dataSourceId: string;
  entityName: string;
  attributeName: string;
  /** 落点角色（自由文本，如 省/市/街道/详细）——抽象字段一对多落点时给 ② 的选列提示。 */
  role?: string;
  /** 语义槽（P1-5 mapping）：过滤条件 / 聚合方式 / 粒度（自由文本，有值才带）。 */
  filter?: string;
  aggregation?: string;
  grain?: string;
  /** 落点是否启用；只有显式 false = 停用（缺省 = 启用）。 */
  enabled?: boolean;
  stale: boolean;
  staleReason?: 'source_gone' | 'entity_gone' | 'attribute_gone' | null;
}

export interface AbstractAttribute {
  name: string;
  displayName?: string;
  description?: string;
  type: string;
  primaryKey: boolean;
  required: boolean;
  enumValues?: string[];
  sources: AbstractAttributeSource[];
}

export interface AbstractEntity {
  name: string;
  displayName?: string;
  description: string;
  aliases: string[];
  sources: AbstractLanding[];
  attributes: AbstractAttribute[];
}

export interface AbstractBacking {
  dataSourceId: string;
  relationName: string;
  sourceColumn?: string | null;
  targetColumn?: string | null;
  origin?: string | null;
  stale: boolean;
  staleReason?: 'source_gone' | 'relation_gone' | null;
}

export interface AbstractRelation {
  name: string;
  source: string;
  target: string;
  cardinality?: string;
  description: string;
  /** 多态端点（实体名列表；长度 >1 才有值；集合必含 source/target）。 */
  sourceTypes?: string[];
  targetTypes?: string[];
  /** 逆关系名（纯声明）。 */
  inverseOf?: string;
  backings: AbstractBacking[];
}

/** 指标落点：跨源口径的登记格（`diMetric` 走 DI /v1/query；`entityName` 走 ② 补位；至少其一）。 */
export interface AbstractMetricLanding {
  dataSourceId: string;
  diMetric: string;
  entityName: string;
}

export interface AbstractMetric {
  name: string;
  displayName?: string;
  description?: string;
  aliases: string[];
  additivity: 'additive' | 'semi_additive' | 'non_additive' | 'unknown';
  landings: AbstractMetricLanding[];
}

export interface AbstractSchemaBody {
  entities: AbstractEntity[];
  relations: AbstractRelation[];
  /** 指标为可缺省键（老数据/构造路径的 body 可能不带）——读侧一律 `?? []`。 */
  metrics?: AbstractMetric[];
}

export interface AbstractSchema {
  id: string;
  knowledgeBaseId: string;
  schemaVersion: number;
  status: 'proposed' | 'active';
  schema: AbstractSchemaBody;
  sourceHash?: string | null;
  editedAt?: string | null;
  editedBy?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  staleCount: number;
}

/** 保存历史列表项（不含 schema 内容）。 */
export interface AbstractSchemaHistoryItem {
  id: string;
  schemaVersion: number;
  status: 'proposed' | 'active';
  savedBy: string | null;
  /** `manual` = 手动保存 / 确认 / 导入；`restore` = 由历史快照还原产生。 */
  source: 'manual' | 'restore';
  createdAt: string | null;
}

/** 保存历史详情（含 schema 内容）。 */
export interface AbstractSchemaHistoryDetail extends AbstractSchemaHistoryItem {
  knowledgeBaseId: string;
  schema: AbstractSchemaBody;
}

export interface AbstractSchemaHistoryList {
  items: AbstractSchemaHistoryItem[];
  total: number;
}

/** 聚焦生成：预览返回的种子（重点实体；partner = 同概念对应，合并的另一半）。 */
export interface FocusSeed {
  dataSourceId: string;
  entityName: string;
  sourceName?: string;
  description?: string;
  role: 'focus' | 'partner';
  concept?: string;
}

/** 提交给 generate 的种子（只带契约字段）。 */
export interface FocusSeedInput {
  dataSourceId: string;
  entityName: string;
  role: 'focus' | 'partner';
}

/** 聚焦邻居的「经由」明细：由哪枚种子、经哪条关系拉进来。 */
export interface FocusVia {
  seedDataSourceId: string;
  seedEntityName: string;
  relationName: string;
}

export interface FocusNeighbor {
  dataSourceId: string;
  entityName: string;
  sourceName?: string;
  via: FocusVia[];
}

export interface FocusPreview {
  seeds: FocusSeed[];
  neighbors: FocusNeighbor[];
  warnings: string[];
}

/** generate 聚焦路径回带的摘要（全量路径没有）。 */
export interface FocusSummary {
  seeds: number;
  partners: number;
  neighbors: number;
  includedTables: number;
  excludedTables: number;
  /** 部分提交种子在源侧已失效被剔除的条数（全部失效时走 1001，不返回摘要）。 */
  droppedSeeds: number;
}

export interface GenerateResult extends AbstractSchema {
  droppedSources: number;
  landings: { entities: number; attributes: number; error: string | null };
  /** 仅聚焦路径存在（无 focus_seeds 的全量生成不返回该字段）。 */
  focus?: FocusSummary;
}

function mapLanding(raw: any): AbstractLanding {
  return {
    dataSourceId: raw?.data_source_id || '',
    entityName: raw?.entity_name || '',
    included: raw?.included !== false,
    sourceName: raw?.source_name ?? null,
    stale: Boolean(raw?.stale),
    staleReason: raw?.stale_reason ?? null,
  };
}

function mapAttribute(raw: any): AbstractAttribute {
  return {
    name: raw?.name || '',
    displayName: raw?.display_name ?? undefined,
    description: raw?.description ?? undefined,
    type: raw?.type || 'string',
    primaryKey: Boolean(raw?.primary_key),
    required: Boolean(raw?.required),
    enumValues: raw?.enum_values ?? undefined,
    sources: (raw?.sources || []).map((s: any) => ({
      dataSourceId: s?.data_source_id || '',
      entityName: s?.entity_name || '',
      attributeName: s?.attribute_name || '',
      role: s?.role ?? undefined,
      // P1-5 语义槽：只在有值时带（缺键不物化成空串/false，往返断言才干净）。
      ...(s?.filter ? { filter: s.filter } : {}),
      ...(s?.aggregation ? { aggregation: s.aggregation } : {}),
      ...(s?.grain ? { grain: s.grain } : {}),
      ...(typeof s?.enabled === 'boolean' ? { enabled: s.enabled } : {}),
      stale: Boolean(s?.stale),
      staleReason: s?.stale_reason ?? null,
    })),
  };
}

function mapEntity(raw: any): AbstractEntity {
  return {
    name: raw?.name || '',
    displayName: raw?.display_name ?? undefined,
    description: raw?.description || '',
    aliases: raw?.aliases || [],
    sources: (raw?.sources || []).map(mapLanding),
    attributes: (raw?.attributes || []).map(mapAttribute),
  };
}

function mapRelation(raw: any): AbstractRelation {
  return {
    name: raw?.name || '',
    source: raw?.source || '',
    target: raw?.target || '',
    cardinality: raw?.cardinality ?? undefined,
    description: raw?.description || '',
    sourceTypes: Array.isArray(raw?.source_types) ? raw.source_types : undefined,
    targetTypes: Array.isArray(raw?.target_types) ? raw.target_types : undefined,
    inverseOf: raw?.inverse_of ?? undefined,
    backings: (raw?.backings || []).map((b: any) => ({
      dataSourceId: b?.data_source_id || '',
      relationName: b?.relation_name || '',
      sourceColumn: b?.source_column ?? null,
      targetColumn: b?.target_column ?? null,
      origin: b?.origin ?? null,
      stale: Boolean(b?.stale),
      staleReason: b?.stale_reason ?? null,
    })),
  };
}

function mapMetricLanding(raw: any): AbstractMetricLanding {
  return {
    dataSourceId: raw?.data_source_id ?? '',
    diMetric: raw?.di_metric ?? '',
    entityName: raw?.entity_name ?? '',
  };
}

function mapMetric(raw: any): AbstractMetric {
  const additivity = raw?.additivity;
  return {
    name: raw?.name ?? '',
    displayName: raw?.display_name ?? undefined,
    description: raw?.description ?? undefined,
    aliases: Array.isArray(raw?.aliases) ? raw.aliases : [],
    additivity: ['additive', 'semi_additive', 'non_additive'].includes(additivity)
      ? additivity
      : 'unknown',
    landings: (raw?.landings || []).map(mapMetricLanding),
  };
}

/** 抽象 schema body 的 snake_case 读映射（保存回包与历史快照详情共用）。 */
function mapSchemaBody(raw: any): AbstractSchemaBody {
  const body = raw && typeof raw === 'object' ? raw : {};
  return {
    entities: (body.entities || []).map(mapEntity),
    relations: (body.relations || []).map(mapRelation),
    metrics: (body.metrics || []).map(mapMetric),
  };
}

/** snake_case 出参：三个写口（save/generate 回包）都用它，逐字段重建（多余键到不了网络）。 */
function mapSchema(raw: any): AbstractSchema | null {
  // 判的是 id 是否存在（网关对 None 的兜底是 `{}`；`{}` 在 JS 里为真值——历史坑，别改回 !raw）
  if (!raw || !raw.id) return null;
  return {
    id: raw.id,
    knowledgeBaseId: raw.knowledge_base_id,
    schemaVersion: raw.schema_version ?? 1,
    status: raw.status === 'active' ? 'active' : 'proposed',
    schema: mapSchemaBody(raw?.schema),
    sourceHash: raw.source_hash ?? null,
    editedAt: raw.edited_at ?? null,
    editedBy: raw.edited_by ?? null,
    createdAt: raw.created_at ?? null,
    updatedAt: raw.updated_at ?? null,
    staleCount: raw.stale_count ?? 0,
  };
}

export async function getAbstractSchema(kbId: string): Promise<AbstractSchema | null> {
  const res = request.get<ApiResponse<any>>('/knowledge-base/ontology/abstract-schema', {
    params: { knowledge_base_id: kbId },
  });
  return mapSchema(await getData(res));
}

/** 聚焦生成·解析预览（不落库；LLM 解析，超时 120s）。 */
export async function previewFocusAbstractSchema(
  kbId: string,
  prompt: string,
): Promise<FocusPreview> {
  const body = await getData<any>(
    request.post('/knowledge-base/ontology/abstract-schema/focus-preview', {
      knowledge_base_id: kbId, prompt,
    }, { timeout: 120000 }),
  );
  return mapFocusPreview(body);
}

/**
 * 生成候选（结果恒为 proposed；已有 active 时后端 409，force 重试）。超时 200s（LLM 全量聚类，同 v1 理由）。
 * `focusSeeds` 非空 = 聚焦路径（只纳入种子闭包）；缺省/空数组 = 现状全量（body 不带 focus_seeds 键，逐字节兼容）。
 */
export async function generateAbstractSchema(
  kbId: string,
  force = false,
  focusSeeds?: FocusSeedInput[],
  options: {
    mode?: 'replace' | 'append';
    seed?: 'document' | 'structured';
    autoLandings?: boolean;
    /** 业务词表：每行 `别名1,别名2 => 实体名或物理表名`，生成后自动挂到对应实体。 */
    aliasHints?: string;
  } = {},
): Promise<GenerateResult> {
  const body = await getData<any>(
    request.post('/knowledge-base/ontology/abstract-schema/generate', {
      knowledge_base_id: kbId,
      force,
      ...(focusSeeds && focusSeeds.length > 0
        ? {
            focus_seeds: focusSeeds.map((s) => ({
              data_source_id: s.dataSourceId,
              entity_name: s.entityName,
              role: s.role,
            })),
          }
        : {}),
      ...(options.mode ? { mode: options.mode } : {}),
      ...(options.seed ? { seed: options.seed } : {}),
      ...(options.autoLandings === true ? { auto_landings: true } : {}),
      ...(options.aliasHints?.trim() ? { alias_hints: options.aliasHints.trim() } : {}),
    }, { timeout: 200000 }),
  );
  return mapGenerateResult(body);
}

export async function saveAbstractSchema(payload: {
  knowledgeBaseId: string;
  schema: AbstractSchemaBody;
  status: 'proposed' | 'active';
  expectedSchemaVersion: number;
}): Promise<AbstractSchema> {
  const body = await getData<any>(
    request.put('/knowledge-base/ontology/abstract-schema', {
      knowledge_base_id: payload.knowledgeBaseId,
      schema: {
        entities: payload.schema.entities.map((e) => ({
          name: e.name,
          display_name: e.displayName,
          description: e.description,
          aliases: e.aliases,
          sources: e.sources.map((s) => ({
            data_source_id: s.dataSourceId,
            entity_name: s.entityName,
            included: s.included,
          })),
          attributes: e.attributes.map((a) => ({
            name: a.name,
            display_name: a.displayName,
            description: a.description,
            type: a.type,
            primary_key: a.primaryKey,
            required: a.required,
            enum_values: a.enumValues,
            sources: a.sources.map((s) => ({
              data_source_id: s.dataSourceId,
              entity_name: s.entityName,
              attribute_name: s.attributeName,
              ...(s.role ? { role: s.role } : {}),
              // P1-5 语义槽：只在有值时写（`enabled` 只认布尔——缺键由后端继承存储行）。
              ...(s.filter ? { filter: s.filter } : {}),
              ...(s.aggregation ? { aggregation: s.aggregation } : {}),
              ...(s.grain ? { grain: s.grain } : {}),
              ...(typeof s.enabled === 'boolean' ? { enabled: s.enabled } : {}),
            })),
          })),
        })),
        relations: payload.schema.relations.map((r) => ({
          name: r.name,
          source: r.source,
          target: r.target,
          cardinality: r.cardinality,
          description: r.description,
          ...(r.inverseOf ? { inverse_of: r.inverseOf } : {}),
          ...(r.sourceTypes && r.sourceTypes.length > 1 ? { source_types: r.sourceTypes } : {}),
          ...(r.targetTypes && r.targetTypes.length > 1 ? { target_types: r.targetTypes } : {}),
          backings: r.backings.map((b) => ({
            data_source_id: b.dataSourceId,
            relation_name: b.relationName,
            source_column: b.sourceColumn,
            target_column: b.targetColumn,
            origin: b.origin,
          })),
        })),
        metrics: (payload.schema.metrics || []).map((m) => ({
          name: m.name,
          display_name: m.displayName,
          description: m.description,
          aliases: m.aliases,
          additivity: m.additivity,
          landings: m.landings.map((l) => ({
            data_source_id: l.dataSourceId,
            di_metric: l.diMetric,
            entity_name: l.entityName,
          })),
        })),
      },
      status: payload.status,
      expected_schema_version: payload.expectedSchemaVersion,
    }),
  );
  return mapSchema(body) as AbstractSchema;
}

function mapHistoryItem(raw: any): AbstractSchemaHistoryItem {
  return {
    id: raw?.id || '',
    schemaVersion: raw?.schema_version ?? 1,
    status: raw?.status === 'active' ? 'active' : 'proposed',
    savedBy: raw?.saved_by ?? null,
    // 只认这两种来源；未知/缺失按手动处理（快照表默认值就是 manual）。
    source: raw?.source === 'restore' ? 'restore' : 'manual',
    createdAt: raw?.created_at ?? null,
  };
}

/** 保存历史列表回包 → 前端形状（缺键安全回落；导出供单测）。 */
export function mapAbstractSchemaHistoryList(raw: any): AbstractSchemaHistoryList {
  const items = Array.isArray(raw?.items) ? raw.items : [];
  return {
    items: items.map(mapHistoryItem),
    total: raw?.total ?? items.length,
  };
}

/** 保存历史详情回包 → 前端形状（含 schema；导出供单测）。 */
export function mapAbstractSchemaHistoryDetail(raw: any): AbstractSchemaHistoryDetail {
  return {
    ...mapHistoryItem(raw),
    knowledgeBaseId: raw?.knowledge_base_id ?? '',
    schema: mapSchemaBody(raw?.schema),
  };
}

/** 该 KB 最近 N 条保存快照（新→旧，默认 20，服务端 clamp 1..50）。 */
export async function listAbstractSchemaHistory(
  kbId: string,
  limit?: number,
): Promise<AbstractSchemaHistoryList> {
  const res = request.get<ApiResponse<any>>('/knowledge-base/ontology/abstract-schema/history', {
    params: {
      knowledge_base_id: kbId,
      ...(typeof limit === 'number' ? { limit } : {}),
    },
  });
  return mapAbstractSchemaHistoryList(await getData(res));
}

/** 单条快照详情（含 schema）。别的租户 / 别的 KB 一律 1002。 */
export async function getAbstractSchemaHistoryDetail(
  snapshotId: string,
  kbId: string,
): Promise<AbstractSchemaHistoryDetail> {
  const res = request.get<ApiResponse<any>>(
    `/knowledge-base/ontology/abstract-schema/history/${snapshotId}`,
    { params: { knowledge_base_id: kbId } },
  );
  return mapAbstractSchemaHistoryDetail(await getData(res));
}

/**
 * 按快照还原（生成一个新的保存版本）。
 * `expectedSchemaVersion` 是**当前行版本号**（乐观锁）：编辑页可能开着很久，
 * 不带版本就是拿旧内容盖新内容——服务端按既有 1003/409 口径拒绝。
 */
export async function restoreAbstractSchemaHistory(
  snapshotId: string,
  kbId: string,
  expectedSchemaVersion: number,
): Promise<AbstractSchema> {
  const body = await getData<any>(
    request.post(`/knowledge-base/ontology/abstract-schema/history/${snapshotId}/restore`, {
      knowledge_base_id: kbId,
      expected_schema_version: expectedSchemaVersion,
    }),
  );
  return mapSchema(body) as AbstractSchema;
}

export interface ImportSummary {
  entitiesAdded: number;
  entitiesUpdated: number;
  attributesAdded: number;
  ignoredSections: string[];
}

export interface ImportTboxResult {
  dryRun: boolean;
  summary: ImportSummary;
  warnings: string[];
  /** 正式导入的 save 回包（新版本行）；dry_run 时为 null。 */
  schema: AbstractSchema | null;
}

/** 导入回包 → 前端形状（缺键安全回落；导出供单测）。 */
export function mapImportResult(raw: any): ImportTboxResult {
  const s = raw?.summary || {};
  return {
    dryRun: Boolean(raw?.dry_run),
    summary: {
      entitiesAdded: s.entities_added ?? 0,
      entitiesUpdated: s.entities_updated ?? 0,
      attributesAdded: s.attributes_added ?? 0,
      ignoredSections: Array.isArray(s.ignored_sections) ? s.ignored_sections : [],
    },
    warnings: Array.isArray(raw?.warnings) ? raw.warnings : [],
    schema: raw?.dry_run ? null : mapSchema(raw),
  };
}

/**
 * 导入共享本体 TBox（YAML）。`dryRun=true` 只算 diff 不写库（免版本）；
 * 正式导入带 `expectedSchemaVersion`——**首次导入传 0**（后端 upsert 对「行不存在」
 * 走 INSERT、不看该值；行已存在时 0 被 CAS 判 1003）。
 */
export async function importAbstractSchema(kbId: string, payload: {
  yamlText: string; expectedSchemaVersion: number; dryRun: boolean;
}): Promise<ImportTboxResult> {
  const body = await getData<any>(
    request.post('/knowledge-base/ontology/abstract-schema/import', {
      knowledge_base_id: kbId,
      yaml_text: payload.yamlText,
      expected_schema_version: payload.expectedSchemaVersion,
      dry_run: payload.dryRun,
    }, { timeout: 60000 }),
  );
  return mapImportResult(body);
}

export interface ConstructResult {
  schemaVersion: number;
  attributesAdded: number;
  relationsAdded: number;
  suggestionsAdded: number;
  namesEnabled: boolean;
  namesPatched: number;
  namesRemaining: number;
  landings: {
    entities: number;
    attributes: number;
    error: string | null;
  };
  warnings: string[];
}

/** 后端摘要 → 前端形状（缺键安全回落；导出供单测）。 */
export function mapConstructResult(raw: any): ConstructResult {
  return {
    schemaVersion: raw?.schema_version ?? 0,
    attributesAdded: raw?.attributes?.added ?? 0,
    relationsAdded: raw?.relations?.added ?? 0,
    suggestionsAdded: raw?.suggestions?.added ?? 0,
    namesEnabled: Boolean(raw?.names?.enabled),
    namesPatched: raw?.names?.patched ?? 0,
    namesRemaining: raw?.names?.remaining ?? 0,
    landings: {
      entities: raw?.landings?.entities ?? 0,
      attributes: raw?.landings?.attributes ?? 0,
      error: typeof raw?.landings?.error === 'string' ? raw.landings.error : null,
    },
    warnings: Array.isArray(raw?.warnings) ? raw.warnings : [],
  };
}

/** 聚焦预览回包 → 前端形状（缺键安全回落，不抛）。 */
export function mapFocusPreview(raw: any): FocusPreview {
  const seeds = Array.isArray(raw?.seeds) ? raw.seeds : [];
  const neighbors = Array.isArray(raw?.neighbors) ? raw.neighbors : [];
  return {
    seeds: seeds.map((s: any) => ({
      dataSourceId: s?.data_source_id ?? '',
      entityName: s?.entity_name ?? '',
      sourceName: s?.source_name ?? '',
      description: s?.description ?? '',
      role: s?.role === 'partner' ? 'partner' : 'focus',
      concept: s?.concept ?? '',
    })),
    neighbors: neighbors.map((n: any) => ({
      dataSourceId: n?.data_source_id ?? '',
      entityName: n?.entity_name ?? '',
      sourceName: n?.source_name ?? '',
      via: (Array.isArray(n?.via) ? n.via : []).map((v: any) => ({
        seedDataSourceId: v?.seed_data_source_id ?? '',
        seedEntityName: v?.seed_entity_name ?? '',
        relationName: v?.relation_name ?? '',
      })),
    })),
    warnings: Array.isArray(raw?.warnings) ? raw.warnings : [],
  };
}

/** 生成回包 → GenerateResult（含可选 focus 摘要）。 */
export function mapGenerateResult(raw: any): GenerateResult {
  const base = mapSchema(raw) as AbstractSchema;
  const f = raw?.focus;
  return {
    ...base,
    droppedSources: raw?.dropped_sources ?? 0,
    landings: {
      entities: raw?.landings?.entities ?? 0,
      attributes: raw?.landings?.attributes ?? 0,
      error: typeof raw?.landings?.error === 'string' ? raw.landings.error : null,
    },
    focus: f
      ? {
          seeds: f.seeds ?? 0,
          partners: f.partners ?? 0,
          neighbors: f.neighbors ?? 0,
          includedTables: f.included_tables ?? 0,
          excludedTables: f.excluded_tables ?? 0,
          droppedSeeds: f.dropped_seeds ?? 0,
        }
      : undefined,
  };
}

/**
 * 自动构造（补齐式）：一次请求完成填充；结果恒为 proposed；已有 active 时后端 409
 * （调用方二次确认后带 force 重发——与概念对齐页 `doGenerate` 同一模式）。
 * 超时口径对齐 generate（200s：确定性阶段 + 名字档预算 + 落库余量）。
 */
export async function constructAbstractSchema(
  kbId: string,
  options: {
    force?: boolean;
    llmNames?: boolean;
    seed?: 'structured' | 'document';
    autoLandings?: boolean;
  } = {},
): Promise<ConstructResult> {
  const body = await getData<any>(
    request.post('/knowledge-base/ontology/abstract-schema/construct', {
      knowledge_base_id: kbId,
      force: options.force ?? false,
      ...(typeof options.llmNames === 'boolean' ? { llm_names: options.llmNames } : {}),
      ...(options.seed ? { seed: options.seed } : {}),
      ...(options.autoLandings === true ? { auto_landings: true } : {}),
    }, { timeout: 200000 }),
  );
  return mapConstructResult(body);
}
