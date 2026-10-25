export type Id = string;
/** 属性类型:与知识库侧 TBox 词表对齐(string/text/number/integer/boolean/date/datetime/enum 共存,不做映射) */
export type AttributeType = 'string' | 'text' | 'number' | 'integer' | 'boolean' | 'date' | 'datetime' | 'enum';
export type Severity = 'error' | 'warning';
/** 关系基数:与知识库侧 TBox 的 relation_types[].cardinality 同词汇 */
export type Cardinality = 'one_to_one' | 'one_to_many' | 'many_to_many' | 'many_to_one' | 'custom';

/** 实体/关系补丁:显式 undefined = 清除该可选字段(命令层 Object.assign 后导出判 undefined 即视为未设置) */
export type EntityPatch = Partial<{ [K in Exclude<keyof EntityType, 'id'>]: EntityType[K] | undefined }>;
export type RelationPatch = Partial<{ [K in Exclude<keyof RelationType, 'id'>]: RelationType[K] | undefined }>;

/** TBox 构建技能声明(知识库侧 extraction_skills:refine/constrain 两组)。
 *  编辑器不解释其内容,只做**原样透传**(未知子键也保留),保证导入→导出不失真。 */
export interface ExtractionSkillRef {
  skill: string;
  description?: string;
  [key: string]: unknown;
}
export interface ExtractionSkills {
  refine?: ExtractionSkillRef[];
  constrain?: ExtractionSkillRef[];
  [key: string]: unknown;
}

/** 属性来源（仅抽象 schema 画布使用；camelCase 镜像 `AbstractAttributeSource`）。 */
export interface AttributeSource {
  dataSourceId: string;
  entityName: string;
  attributeName: string;
  /** 落点角色（自由文本；TBox 导入线加）。 */
  role?: string;
  /** 语义槽（P1-5 mapping 对象化）：过滤条件 / 聚合方式 / 粒度（自由文本，有值才带）。 */
  filter?: string;
  aggregation?: string;
  grain?: string;
  /** 落点是否启用；只有显式 false = 停用（缺省 = 启用）。 */
  enabled?: boolean;
  stale?: boolean;
  staleReason?: string | null;
}

/** 实体落点（仅抽象 schema 画布使用；编译画布不设此字段）。 */
export interface EntityLanding {
  dataSourceId: string;
  entityName: string;
  included: boolean;
  sourceName?: string | null;
  stale?: boolean;
  staleReason?: string | null;
}

/** 关系物理背书（仅抽象 schema 画布使用）。空 backings = 逻辑关系。 */
export interface RelationBacking {
  dataSourceId: string;
  relationName: string;
  sourceColumn?: string | null;
  targetColumn?: string | null;
  origin?: string | null;
  stale?: boolean;
  staleReason?: string | null;
}

export interface Attribute {
  id: Id;
  name: string;
  type: AttributeType;
  required: boolean;
  primaryKey: boolean;
  description?: string;
  enumValues?: string[];
  /** 本地化显示名（仅抽象 schema 画布使用；编译画布不设此字段）。 */
  displayName?: string;
  /** 属性来源（仅抽象 schema 画布使用；编译画布不设此字段）。 */
  sources?: AttributeSource[];
}

export interface EntityType {
  id: Id;
  name: string;
  displayName?: string;
  aliases: string[];
  description?: string;
  attributes: Attribute[];
  /** TBox 技能声明(导入保留、导出原样写回;编辑器不解释) */
  extractionSkills?: ExtractionSkills;
  /** 源落点（仅抽象 schema 画布使用；编译画布不设此字段）。 */
  landings?: EntityLanding[];
}

export interface RelationType {
  id: Id;
  name: string;
  sourceEntityId: Id;
  targetEntityId: Id;
  description?: string;
  /** 多态端点(内部按实体 id 存;TBox 里的类型名在导入时解析成 id)。
   *  为空 = 单值回落 source/targetEntityId —— 知识库侧 `_post_validate` 按集合判定端点,
   *  只声明单值会让端点类型不符的边被丢。 */
  sourceTypes?: Id[];
  targetTypes?: Id[];
  /** 逆关系名(纯声明,物理只存 canonical 方向) */
  inverseOf?: string;
  cardinality?: Cardinality;
  /** 物理背书（仅抽象 schema 画布使用；编译画布不设此字段）。空 = 逻辑关系。 */
  backings?: RelationBacking[];
}

interface ConstraintBase {
  id: Id;
  severity: Severity;
}

export interface RequiredAttributeConstraint extends ConstraintBase {
  type: 'required_attribute';
  entityId: Id;
  attributeId: Id;
}

export interface UniqueAttributeConstraint extends ConstraintBase {
  type: 'unique_attribute';
  entityId: Id;
  attributeId: Id;
}

export interface ValueRangeConstraint extends ConstraintBase {
  type: 'value_range';
  entityId: Id;
  attributeId: Id;
  min?: number | string;
  max?: number | string;
}

export interface RelationCardinalityConstraint extends ConstraintBase {
  type: 'relation_cardinality';
  relationId: Id;
  min: number;
  max: number | 'unbounded';
}

export interface QueryRuleConstraint extends ConstraintBase {
  type: 'query_rule';
  entityId: Id;
  attributeId: Id;
  /** 口径长文本（服务端 `constraints[].expression` 直传） */
  expression: string;
}

export type Constraint =
  | RequiredAttributeConstraint
  | UniqueAttributeConstraint
  | ValueRangeConstraint
  | RelationCardinalityConstraint
  | QueryRuleConstraint;

export interface OntologyModel {
  entities: EntityType[];
  relations: RelationType[];
  constraints: Constraint[];
  disambiguation: {
    caseInsensitive: boolean;
    aliasMerge: boolean;
  };
}

export interface EditorMetadata {
  nodePositions: Record<Id, { x: number; y: number }>;
  viewport: { x: number; y: number; zoom: number };
}

export interface DomainDocument {
  storageVersion: 1;
  schemaVersion: 1;
  revision: number;
  domain: {
    id: string;
    displayName: string;
    description?: string;
  };
  model: OntologyModel;
  editor: EditorMetadata;
  savedAt: string;
}

export interface DomainSummary {
  id: string;
  displayName: string;
  description?: string;
  schemaVersion: 1;
  entityCount: number;
  relationCount: number;
  ruleCount: number;
  updatedAt: string;
  revision: number;
}

export type ExportIssueLevel = 'mustFix' | 'confirm';
export type ExportTargetKind = 'domain' | 'entity' | 'attribute' | 'relation' | 'constraint';

export interface ExportIssue {
  code: string;
  level: ExportIssueLevel;
  target?: {
    kind: ExportTargetKind;
    id: string;
    field?: string;
  };
  params?: Record<string, string | number>;
}

export interface ExportAttribute {
  name: string;
  type: AttributeType;
  required: boolean;
  /** 知识库侧 TBox 键名(此前写 primary_key,与 TBox 不一致;导入两写法都认) */
  is_primary_key?: true;
  /** 本仓历史导出键名(仅导入时识别,导出不再写) */
  primary_key?: true;
  description?: string;
  enum_values?: string[];
}

export interface ExportEntityType {
  name: string;
  /** 本地化显示名(此前导出丢失) */
  display_name?: string;
  aliases: string[];
  description?: string;
  attributes: ExportAttribute[];
  /** TBox 技能声明(原样透传) */
  extraction_skills?: ExtractionSkills;
}

export interface ExportRelationType {
  name: string;
  source: string;
  target: string;
  description?: string;
  /** 多态端点(写实体名;为空不写 = 单值) */
  source_types?: string[];
  target_types?: string[];
  inverse_of?: string;
  cardinality?: Cardinality;
}

export type ExportConstraint =
  | { type: 'required_attribute' | 'unique_attribute'; entity: string; attribute: string; severity: Severity }
  | { type: 'value_range'; entity: string; attribute: string; min?: number | string; max?: number | string; severity: Severity }
  | { type: 'relation_cardinality'; relation: string; min: number; max: number | 'unbounded'; severity: Severity };

export interface ExportSchemaV1 {
  version: 1;
  domain: string;
  entity_types: ExportEntityType[];
  relation_types: ExportRelationType[];
  constraints: ExportConstraint[];
  disambiguation: {
    case_insensitive: boolean;
    alias_merge: boolean;
  };
}
