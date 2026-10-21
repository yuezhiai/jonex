/**
 * 数据治理第 5 期「标准目录」的对外类型（页面只见 camelCase）。
 *
 * 后端标准目录读接口返回 snake_case；转换全部收敛在 `api/standardCatalog.ts`。
 * `version` / `published_at` 一律表示**最近发布值**，目录展示的是当前工作副本，
 * 两者可能不一致（页面负责提示，不在类型层伪造一致性）。
 */

/** 文档统计：docTotal 是分母，其余四项是不同判定结果的分桶。 */
export interface DgDocStats {
  docTotal: number;
  noTarget: number;
  notApplied: number;
  appliedCurrent: number;
  ontologyReady: number;
}

/** 标准领域（列表行带场景数/对象数/最近发布时间，详情行不带计数）。 */
export interface DgStandardDomain {
  id: string;
  name: string;
  nameEn: string | null;
  description: string | null;
  status: string | null;
  version: number | null;
  publishedAt: string | null;
  structureHash: string | null;
  scenarioCount?: number;
  objectCount?: number;
  latestPublishedAt?: string | null;
}

/** 标准场景（领域详情行带四类计数，场景详情头部不带）。 */
export interface DgStandardScenario {
  id: string;
  domainId?: string;
  name: string;
  description?: string | null;
  version: number | null;
  publishedAt: string | null;
  structureHash: string | null;
  objectCount?: number;
  attributeCount?: number;
  relationCount?: number;
  constraintCount?: number;
}

/** 场景对象属性；`is_primary_key` / `is_required` 已由后端归一并由映射层转 boolean。 */
export interface DgStandardAttribute {
  id: string;
  attrName: string;
  description: string;
  attrType: string | null;
  isPrimaryKey: boolean;
  isRequired: boolean;
  ontologyCode: string | null;
  constraintsJson: Record<string, unknown>;
}

/** 场景对象（含嵌套属性）。 */
export interface DgStandardObject {
  id: string;
  name: string;
  description: string;
  status: string | null;
  ontologyCode: string | null;
  aliases: string[];
  attributes: DgStandardAttribute[];
}

/** 场景关系。 */
export interface DgStandardRelation {
  id: string;
  name: string;
  description: string;
  relationType: string | null;
  status: string | null;
  sourceObjectId: string | null;
  targetObjectId: string | null;
  ontologyCode: string | null;
}

/** 场景约束（本期模板约束未编译进执行链路，仅目录展示）。 */
export interface DgStandardConstraint {
  id: string;
  name: string;
  targetType: string;
  targetId: string;
  targetLabel: string;
  constraintType: string;
  expression: string | null;
  suggestion: string | null;
}

/** 知识库应用行：绑定侧与 compiled 侧来源分别保留，不互相顶替。 */
export interface DgKbSchema {
  kbId: string;
  kbName: string;
  spaceId: string | null;
  scenarioId: string | null;
  scenarioName: string | null;
  bindingScenarioId: string | null;
  compiledScenarioId: string | null;
  domainId: string | null;
  domainName: string | null;
  bindingStatus: string;
  sourceType: string | null;
  sourceVersion: number | null;
  sourceHash: string | null;
  schemaVersion: number | null;
  schemaMode: string | null;
  syncStatus: string | null;
  compiledAt: string | null;
  docStats: DgDocStats;
}

/** 场景/对象/属性的引用 KB 行（比列表行少 scenario/domain 字段）。 */
export interface DgStandardReference {
  kbId: string;
  kbName: string;
  spaceId: string | null;
  bindingStatus: string;
  sourceType: string | null;
  bindingScenarioId: string | null;
  compiledScenarioId: string | null;
  sourceVersion: number | null;
  schemaVersion: number | null;
  syncStatus: string | null;
  schemaMode: string | null;
  docStats: DgDocStats;
}

/**
 * 引用 KB 查询结果。
 *
 * - `truncated`：结果超过后端上限（200）时为 true；
 * - `sourceIdCoverage`：`partial` 表示 object/attribute 维度仅统计记录了来源 ID 的
 *   schema，可能漏计，页面需展示提示。
 */
export interface DgStandardReferencesResult {
  scopeType: 'scenario' | 'object' | 'attribute';
  scopeId: string;
  items: DgStandardReference[];
  total: number;
  truncated: boolean;
  sourceIdCoverage: 'full' | 'partial';
}

/* ── 发布版本历史与 diff（第 6 项第二增量） ───────────────────────── */

/** 发布版本摘要行（列表用，不含快照）。 */
export interface DgScenarioVersion {
  id: string;
  version: number;
  structureHash: string | null;
  publishedBy: string | null;
  publishedAt: string | null;
}

/** 指定版本的完整快照（结构由后端 build_scenario_snapshot 决定）。 */
export interface DgScenarioVersionDetail extends DgScenarioVersion {
  snapshot: Record<string, unknown>;
}

/** 单字段变更。`from` / `to` 为后端原始值（可能为 null / 0/1）。 */
export interface DgFieldChange {
  field: string;
  from: unknown;
  to: unknown;
}

/** 单个实体的增删改；`details` 是后端附带的标量字段。 */
export interface DgEntityChange {
  id: string;
  label: string;
  fields?: DgFieldChange[];
  details: Record<string, unknown>;
}

export interface DgEntityDiff {
  added: DgEntityChange[];
  removed: DgEntityChange[];
  changed: DgEntityChange[];
}

/** 两个发布版本的差异。 */
export interface DgScenarioVersionDiff {
  fromVersion: number;
  toVersion: number;
  scenario: { changed: DgFieldChange[] };
  objects: DgEntityDiff;
  attributes: DgEntityDiff;
  relations: DgEntityDiff;
  constraints: DgEntityDiff;
  summary: Record<string, number>;
}

/* ── 落标校验（标准侧第二增量） ───────────────────────────────────── */

export interface DgComplianceFieldChange {
  field: string;
  standard: unknown;
  compiled: unknown;
}

export interface DgComplianceEntity {
  id: string;
  label: string;
  /** 属性行才有：所属对象标签。 */
  object?: string;
  /** changed_attributes 行才有。 */
  fields?: DgComplianceFieldChange[];
}

export type DgComplianceStatus =
  | 'current'
  | 'outdated'
  | 'mismatch'
  | 'no_schema'
  | 'no_source_version'
  | 'not_published';

export interface DgKbComplianceItem {
  kbId: string;
  kbName: string | null;
  status: DgComplianceStatus;
  latestVersion: number | null;
  sourceVersion: number | null;
  schemaVersion: number | null;
  syncStatus: string | null;
  schemaMode: string | null;
  compiledAt: string | null;
  missingObjects: DgComplianceEntity[];
  extraObjects: DgComplianceEntity[];
  missingAttributes: DgComplianceEntity[];
  extraAttributes: DgComplianceEntity[];
  changedAttributes: DgComplianceEntity[];
  docStats: DgDocStats;
}

export interface DgComplianceResult {
  scenarioId: string;
  latestPublishedVersion: number | null;
  latestStructureHash: string | null;
  items: DgKbComplianceItem[];
  summary: Record<string, number>;
}
