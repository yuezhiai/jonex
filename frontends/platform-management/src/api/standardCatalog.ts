/**
 * 数据治理第 5 期「标准目录」取数模块。
 *
 * 后端 5 个读接口：
 *   GET /data-governance/standards/domains
 *   GET /data-governance/standards/domains/{domain_id}
 *   GET /data-governance/standards/scenarios/{scenario_id}
 *   GET /data-governance/standards/kb-schemas
 *   GET /data-governance/standards/references
 *
 * 约定：**所有后端 snake_case 只在本层转换**，页面与组件只接触 camelCase。
 * 映射函数是纯函数（直接可测），async 函数只负责拼路径/参数并调用 `apiClient`。
 */
import apiClient from './request';
import type {
  DgDocStats,
  DgKbSchema,
  DgStandardAttribute,
  DgStandardConstraint,
  DgStandardDomain,
  DgStandardObject,
  DgStandardReference,
  DgStandardReferencesResult,
  DgStandardRelation,
  DgStandardScenario,
  DgEntityChange,
  DgEntityDiff,
  DgFieldChange,
  DgScenarioVersion,
  DgScenarioVersionDetail,
  DgScenarioVersionDiff,
  DgComplianceEntity,
  DgComplianceResult,
  DgKbComplianceItem,
} from '@/pages/DataGovernance/standardTypes';

// ── 后端响应行（snake_case，仅本层可见） ──────────────────────────────

interface DomainRow {
  id: string;
  name: string;
  name_en?: string | null;
  description?: string | null;
  status?: string | null;
  version?: number | null;
  published_at?: string | null;
  structure_hash?: string | null;
  scenario_count?: number;
  object_count?: number;
  latest_published_at?: string | null;
}

interface ScenarioRow {
  id: string;
  domain_id?: string;
  name: string;
  description?: string | null;
  version?: number | null;
  published_at?: string | null;
  structure_hash?: string | null;
  object_count?: number;
  attribute_count?: number;
  relation_count?: number;
  constraint_count?: number;
}

interface StandardAttributeRow {
  id: string;
  attr_name: string;
  description?: string | null;
  attr_type?: string | null;
  is_primary_key?: number | boolean;
  is_required?: number | boolean;
  ontology_code?: string | null;
  constraints_json?: Record<string, unknown> | null;
}

interface StandardObjectRow {
  id: string;
  name: string;
  description?: string | null;
  status?: string | null;
  ontology_code?: string | null;
  aliases?: string[] | null;
  attributes?: StandardAttributeRow[];
}

interface StandardRelationRow {
  id: string;
  name: string;
  description?: string | null;
  relation_type?: string | null;
  status?: string | null;
  source_object_id?: string | null;
  target_object_id?: string | null;
  ontology_code?: string | null;
}

interface StandardConstraintRow {
  id: string;
  name: string;
  target_type: string;
  target_id: string;
  target_label: string;
  constraint_type: string;
  expression?: string | null;
  suggestion?: string | null;
}

/** KB schema 行：列表接口与引用接口共用；引用侧只是少给 scenario/domain 字段。 */
interface KbSchemaRow {
  kb_id: string;
  kb_name: string;
  space_id?: string | null;
  scenario_id?: string | null;
  scenario_name?: string | null;
  binding_scenario_id?: string | null;
  compiled_scenario_id?: string | null;
  domain_id?: string | null;
  domain_name?: string | null;
  binding_status?: string | null;
  source_type?: string | null;
  source_version?: number | null;
  source_hash?: string | null;
  schema_version?: number | null;
  schema_mode?: string | null;
  sync_status?: string | null;
  compiled_at?: string | null;
  doc_stats?: Record<string, number> | null;
}

// ── 纯函数：响应映射 ─────────────────────────────────────────────────

/** 文档统计：五键缺省一律 0，行里的额外键不外泄。 */
export function mapDocStats(
  row?: Record<string, number> | null,
): DgDocStats {
  return {
    docTotal: row?.doc_total ?? 0,
    noTarget: row?.no_target ?? 0,
    notApplied: row?.not_applied ?? 0,
    appliedCurrent: row?.applied_current ?? 0,
    ontologyReady: row?.ontology_ready ?? 0,
  };
}

export function mapDomain(row: DomainRow): DgStandardDomain {
  return {
    id: row.id,
    name: row.name,
    nameEn: row.name_en ?? null,
    description: row.description ?? null,
    status: row.status ?? null,
    version: row.version ?? null,
    publishedAt: row.published_at ?? null,
    structureHash: row.structure_hash ?? null,
    // 可选计数只在行里真的给了该键时才写入：列表行有、详情行没有，
    // 无条件补 0 会把「后端未统计」伪装成「统计结果为 0」。
    ...(row.scenario_count !== undefined ? { scenarioCount: row.scenario_count } : {}),
    ...(row.object_count !== undefined ? { objectCount: row.object_count } : {}),
    ...(row.latest_published_at !== undefined
      ? { latestPublishedAt: row.latest_published_at }
      : {}),
  };
}

export function mapScenario(row: ScenarioRow): DgStandardScenario {
  return {
    id: row.id,
    domainId: row.domain_id,
    name: row.name,
    description: row.description ?? null,
    version: row.version ?? null,
    publishedAt: row.published_at ?? null,
    structureHash: row.structure_hash ?? null,
    // 与 mapDomain 同口径：计数键缺失时不写，避免把「未统计」显示成 0。
    ...(row.object_count !== undefined ? { objectCount: row.object_count } : {}),
    ...(row.attribute_count !== undefined ? { attributeCount: row.attribute_count } : {}),
    ...(row.relation_count !== undefined ? { relationCount: row.relation_count } : {}),
    ...(row.constraint_count !== undefined
      ? { constraintCount: row.constraint_count }
      : {}),
  };
}

/** `is_primary_key` / `is_required` 在 DB 是 0/1，这里统一归一为 boolean。 */
export function mapAttribute(attr: StandardAttributeRow): DgStandardAttribute {
  return {
    id: attr.id,
    attrName: attr.attr_name,
    description: attr.description ?? '',
    attrType: attr.attr_type ?? null,
    isPrimaryKey: Boolean(attr.is_primary_key),
    isRequired: Boolean(attr.is_required),
    ontologyCode: attr.ontology_code ?? null,
    constraintsJson: attr.constraints_json ?? {},
  };
}

export function mapObject(row: StandardObjectRow): DgStandardObject {
  return {
    id: row.id,
    name: row.name,
    description: row.description ?? '',
    status: row.status ?? null,
    ontologyCode: row.ontology_code ?? null,
    aliases: row.aliases ?? [],
    attributes: (row.attributes ?? []).map(mapAttribute),
  };
}

export function mapRelation(row: StandardRelationRow): DgStandardRelation {
  return {
    id: row.id,
    name: row.name,
    description: row.description ?? '',
    relationType: row.relation_type ?? null,
    status: row.status ?? null,
    sourceObjectId: row.source_object_id ?? null,
    targetObjectId: row.target_object_id ?? null,
    ontologyCode: row.ontology_code ?? null,
  };
}

export function mapConstraint(row: StandardConstraintRow): DgStandardConstraint {
  return {
    id: row.id,
    name: row.name,
    targetType: row.target_type,
    targetId: row.target_id,
    targetLabel: row.target_label,
    constraintType: row.constraint_type,
    expression: row.expression ?? null,
    suggestion: row.suggestion ?? null,
  };
}

/** 列表/详情行：绑定状态缺省 `none`（没有生效绑定），doc_stats 缺省全 0。 */
export function mapKbSchema(row: KbSchemaRow): DgKbSchema {
  return {
    kbId: row.kb_id,
    kbName: row.kb_name,
    spaceId: row.space_id ?? null,
    scenarioId: row.scenario_id ?? null,
    scenarioName: row.scenario_name ?? null,
    bindingScenarioId: row.binding_scenario_id ?? null,
    compiledScenarioId: row.compiled_scenario_id ?? null,
    domainId: row.domain_id ?? null,
    domainName: row.domain_name ?? null,
    bindingStatus: row.binding_status || 'none',
    sourceType: row.source_type ?? null,
    sourceVersion: row.source_version ?? null,
    sourceHash: row.source_hash ?? null,
    schemaVersion: row.schema_version ?? null,
    schemaMode: row.schema_mode ?? null,
    syncStatus: row.sync_status ?? null,
    compiledAt: row.compiled_at ?? null,
    docStats: mapDocStats(row.doc_stats),
  };
}

/** 引用行：只取与影响分析相关的字段，行里多给的 scenario/domain 字段一律忽略。 */
export function mapReference(row: KbSchemaRow): DgStandardReference {
  return {
    kbId: row.kb_id,
    kbName: row.kb_name,
    spaceId: row.space_id ?? null,
    bindingStatus: row.binding_status || 'none',
    sourceType: row.source_type ?? null,
    bindingScenarioId: row.binding_scenario_id ?? null,
    compiledScenarioId: row.compiled_scenario_id ?? null,
    sourceVersion: row.source_version ?? null,
    schemaVersion: row.schema_version ?? null,
    syncStatus: row.sync_status ?? null,
    schemaMode: row.schema_mode ?? null,
    docStats: mapDocStats(row.doc_stats),
  };
}

// ── 接口封装 ─────────────────────────────────────────────────────────

const STANDARDS_PATH = '/data-governance/standards';

export async function listStandardDomains(): Promise<DgStandardDomain[]> {
  const data = await apiClient.get<{ items?: DomainRow[] }>(
    `${STANDARDS_PATH}/domains`,
  );
  return (data.items ?? []).map(mapDomain);
}

export async function getStandardDomain(domainId: string): Promise<{
  domain: DgStandardDomain;
  scenarios: DgStandardScenario[];
}> {
  const data = await apiClient.get<{
    domain: DomainRow;
    scenarios?: ScenarioRow[];
  }>(`${STANDARDS_PATH}/domains/${encodeURIComponent(domainId)}`);
  return {
    domain: mapDomain(data.domain),
    scenarios: (data.scenarios ?? []).map(mapScenario),
  };
}

export async function getStandardScenario(scenarioId: string): Promise<{
  scenario: DgStandardScenario;
  objects: DgStandardObject[];
  relations: DgStandardRelation[];
  constraints: DgStandardConstraint[];
}> {
  const data = await apiClient.get<{
    scenario: ScenarioRow;
    objects?: StandardObjectRow[];
    relations?: StandardRelationRow[];
    constraints?: StandardConstraintRow[];
  }>(`${STANDARDS_PATH}/scenarios/${encodeURIComponent(scenarioId)}`);
  return {
    scenario: mapScenario(data.scenario),
    objects: (data.objects ?? []).map(mapObject),
    relations: (data.relations ?? []).map(mapRelation),
    constraints: (data.constraints ?? []).map(mapConstraint),
  };
}

/**
 * 知识库应用列表。
 *
 * - `kb_id` 去空白后为空则视为未提供，不传；
 * - `keyword` 去空白后为空则不传（后端按「有值才加条件」处理）；
 * - 分页参数原样透传（页面负责合法值，后端仍会校验）。
 */
export async function listKbSchemas(params: {
  kbId?: string;
  keyword?: string;
  page: number;
  pageSize: number;
}): Promise<{ items: DgKbSchema[]; total: number }> {
  const kbId = params.kbId?.trim();
  const keyword = params.keyword?.trim();
  const data = await apiClient.get<{ items?: KbSchemaRow[]; total?: number }>(
    `${STANDARDS_PATH}/kb-schemas`,
    {
      params: {
        ...(kbId ? { kb_id: kbId } : {}),
        ...(keyword ? { keyword } : {}),
        page: params.page,
        page_size: params.pageSize,
      },
    },
  );
  return {
    items: (data.items ?? []).map(mapKbSchema),
    total: data.total ?? 0,
  };
}

export async function getStandardReferences(
  scopeType: 'scenario' | 'object' | 'attribute',
  scopeId: string,
): Promise<DgStandardReferencesResult> {
  const data = await apiClient.get<{
    scope_type: 'scenario' | 'object' | 'attribute';
    scope_id: string;
    items?: KbSchemaRow[];
    total?: number;
    truncated?: boolean;
    source_id_coverage?: 'full' | 'partial';
  }>(`${STANDARDS_PATH}/references`, {
    params: { scope_type: scopeType, scope_id: scopeId },
  });
  return {
    scopeType: data.scope_type,
    scopeId: data.scope_id,
    items: (data.items ?? []).map(mapReference),
    total: data.total ?? 0,
    truncated: Boolean(data.truncated),
    sourceIdCoverage: data.source_id_coverage ?? 'partial',
  };
}

// ── 发布版本历史与 diff（第 6 项第二增量） ────────────────────────────

interface ScenarioVersionRow {
  id: string;
  version: number;
  structure_hash?: string | null;
  snapshot?: Record<string, unknown>;
  published_by?: string | null;
  published_at?: string | null;
}

interface FieldChangeRow {
  field: string;
  from?: unknown;
  to?: unknown;
}

interface EntityChangeRow {
  id: string;
  label?: string;
  fields?: FieldChangeRow[];
  [key: string]: unknown;
}

interface EntityDiffRow {
  added?: EntityChangeRow[];
  removed?: EntityChangeRow[];
  changed?: EntityChangeRow[];
}

interface VersionDiffRow {
  from_version: number;
  to_version: number;
  scenario?: { changed?: FieldChangeRow[] };
  objects?: EntityDiffRow;
  attributes?: EntityDiffRow;
  relations?: EntityDiffRow;
  constraints?: EntityDiffRow;
  summary?: Record<string, number>;
}

export function mapScenarioVersion(row: ScenarioVersionRow): DgScenarioVersion {
  return {
    id: row.id,
    version: row.version,
    structureHash: row.structure_hash ?? null,
    publishedBy: row.published_by ?? null,
    publishedAt: row.published_at ?? null,
  };
}

export function mapScenarioVersionDetail(
  row: ScenarioVersionRow,
): DgScenarioVersionDetail {
  return {
    ...mapScenarioVersion(row),
    snapshot: row.snapshot ?? {},
  };
}

function mapFieldChange(row: FieldChangeRow): DgFieldChange {
  return { field: row.field, from: row.from ?? null, to: row.to ?? null };
}

/** added/removed 行附带标量字段（如 name/attr_name）；统一收进 `details`。 */
function mapEntityChange(row: EntityChangeRow): DgEntityChange {
  const { id, label, fields, ...details } = row;
  const name = row.name;
  const attrName = row.attr_name;
  return {
    id,
    label:
      label ??
      ((typeof name === 'string' && name) ||
        (typeof attrName === 'string' && attrName) ||
        id),
    ...(fields ? { fields: fields.map(mapFieldChange) } : {}),
    details,
  };
}

function mapEntityDiff(row: EntityDiffRow | undefined): DgEntityDiff {
  return {
    added: (row?.added ?? []).map(mapEntityChange),
    removed: (row?.removed ?? []).map(mapEntityChange),
    changed: (row?.changed ?? []).map(mapEntityChange),
  };
}

export function mapVersionDiff(row: VersionDiffRow): DgScenarioVersionDiff {
  return {
    fromVersion: row.from_version,
    toVersion: row.to_version,
    scenario: { changed: (row.scenario?.changed ?? []).map(mapFieldChange) },
    objects: mapEntityDiff(row.objects),
    attributes: mapEntityDiff(row.attributes),
    relations: mapEntityDiff(row.relations),
    constraints: mapEntityDiff(row.constraints),
    summary: row.summary ?? {},
  };
}

/** 场景发布版本列表（不含快照）。 */
export async function listScenarioVersions(
  scenarioId: string,
): Promise<{ items: DgScenarioVersion[]; total: number }> {
  const data = await apiClient.get<{
    items?: ScenarioVersionRow[];
    total?: number;
  }>(`${STANDARDS_PATH}/scenarios/${encodeURIComponent(scenarioId)}/versions`);
  return {
    items: (data.items ?? []).map(mapScenarioVersion),
    total: data.total ?? (data.items ?? []).length,
  };
}

/** 指定发布版本（含快照）。 */
export async function getScenarioVersion(
  scenarioId: string,
  version: number,
): Promise<DgScenarioVersionDetail> {
  const data = await apiClient.get<ScenarioVersionRow>(
    `${STANDARDS_PATH}/scenarios/${encodeURIComponent(scenarioId)}/versions/${version}`,
  );
  return mapScenarioVersionDetail(data);
}

/** 两个发布版本的差异。 */
export async function diffScenarioVersions(
  scenarioId: string,
  fromVersion: number,
  toVersion: number,
): Promise<DgScenarioVersionDiff> {
  const data = await apiClient.get<VersionDiffRow>(
    `${STANDARDS_PATH}/scenarios/${encodeURIComponent(scenarioId)}/versions-diff`,
    { params: { from_version: fromVersion, to_version: toVersion } },
  );
  return mapVersionDiff(data);
}

// ── 落标校验（标准侧第二增量） ──────────────────────────────────────

interface ComplianceEntityRow {
  id: string;
  label?: string;
  object?: string;
  fields?: Array<{ field: string; standard?: unknown; compiled?: unknown }>;
}

interface ComplianceItemRow {
  kb_id: string;
  kb_name?: string | null;
  status: DgKbComplianceItem['status'];
  latest_version?: number | null;
  source_version?: number | null;
  schema_version?: number | null;
  sync_status?: string | null;
  schema_mode?: string | null;
  compiled_at?: string | null;
  missing_objects?: ComplianceEntityRow[];
  extra_objects?: ComplianceEntityRow[];
  missing_attributes?: ComplianceEntityRow[];
  extra_attributes?: ComplianceEntityRow[];
  changed_attributes?: ComplianceEntityRow[];
  doc_stats?: Record<string, number> | null;
}

interface ComplianceRow {
  scenario_id: string;
  latest_published_version?: number | null;
  latest_structure_hash?: string | null;
  items?: ComplianceItemRow[];
  summary?: Record<string, number>;
}

function mapComplianceEntity(row: ComplianceEntityRow): DgComplianceEntity {
  return {
    id: row.id,
    label: row.label ?? row.id,
    ...(row.object ? { object: row.object } : {}),
    ...(row.fields
      ? {
          fields: row.fields.map((field) => ({
            field: field.field,
            standard: field.standard ?? null,
            compiled: field.compiled ?? null,
          })),
        }
      : {}),
  };
}

export function mapComplianceItem(row: ComplianceItemRow): DgKbComplianceItem {
  const mapList = (list?: ComplianceEntityRow[]) =>
    (list ?? []).map(mapComplianceEntity);
  return {
    kbId: row.kb_id,
    kbName: row.kb_name ?? null,
    status: row.status,
    latestVersion: row.latest_version ?? null,
    sourceVersion: row.source_version ?? null,
    schemaVersion: row.schema_version ?? null,
    syncStatus: row.sync_status ?? null,
    schemaMode: row.schema_mode ?? null,
    compiledAt: row.compiled_at ?? null,
    missingObjects: mapList(row.missing_objects),
    extraObjects: mapList(row.extra_objects),
    missingAttributes: mapList(row.missing_attributes),
    extraAttributes: mapList(row.extra_attributes),
    changedAttributes: mapList(row.changed_attributes),
    docStats: mapDocStats(row.doc_stats),
  };
}

export function mapCompliance(row: ComplianceRow): DgComplianceResult {
  return {
    scenarioId: row.scenario_id,
    latestPublishedVersion: row.latest_published_version ?? null,
    latestStructureHash: row.latest_structure_hash ?? null,
    items: (row.items ?? []).map(mapComplianceItem),
    summary: row.summary ?? {},
  };
}

/** 落标校验：场景下各 KB compiled schema 对照最新发布标准。 */
export async function getStandardCompliance(
  scenarioId: string,
): Promise<DgComplianceResult> {
  const data = await apiClient.get<ComplianceRow>(
    `${STANDARDS_PATH}/scenarios/${encodeURIComponent(scenarioId)}/compliance`,
  );
  return mapCompliance(data);
}

// ── 版本回滚（恢复工作副本并发布新版本） ─────────────────────────────

interface RollbackRow {
  scenario_id: string;
  restored_from_version: number;
  published?: { version?: number; structure_hash?: string };
  counts?: Record<string, unknown>;
}

export async function rollbackScenarioVersion(
  scenarioId: string,
  version: number,
): Promise<{
  scenarioId: string;
  restoredFromVersion: number;
  publishedVersion: number | null;
}> {
  const data = await apiClient.post<RollbackRow>(
    `/ecosystem/templates/scenarios/${encodeURIComponent(scenarioId)}/versions/${version}/rollback`,
  );
  return {
    scenarioId: data.scenario_id,
    restoredFromVersion: data.restored_from_version,
    publishedVersion: data.published?.version ?? null,
  };
}
