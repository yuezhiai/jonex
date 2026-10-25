/**
 * Jonex 编译 schema ↔ nexonto DomainDocument 的适配层。
 *
 * 两边的数据形状**不一样**，这里是唯一的转换点：
 *
 * | | Jonex（服务端） | nexonto（画布） |
 * |---|---|---|
 * | 引用方式 | 按**名字**（`source: "Asset"`） | 按**id**（`sourceEntityId: "e:Asset"`） |
 * | 主键 | `is_primary_key: boolean` | `primaryKey: boolean` |
 * | 关系逆名 | `inverse_of` | `inverseOf` |
 * | 显示名 | `display_name` | `displayName` |
 * | 约束目标 | 按名字 | 按 id |
 * | 节点坐标 | **无此概念** | `editor.nodePositions` |
 * | 枚举取值 | **无此字段** | `enumValues` |
 *
 * ## id 从名字派生，而不是随机生成
 *
 * nexonto 的 `newId()` 生成 uuid。但服务端只认名字，id 落库没有意义——
 * 每次加载都换一批 id 的话，React Flow 会把整张图当成全新节点重建。
 * 所以这里**从名字派生稳定 id**：同一个 schema 反复加载得到同一批 id。
 *
 * 副作用：在画布上**重命名**实体时 id 不变（id 是加载时派的），保存回名字即可。
 */

import type {
  Attribute, AttributeType, Cardinality, Constraint, DomainDocument, EntityType,
  OntologyModel, RelationType,
} from './domain/index.js';
import { buildDocumentFromModel } from './domain/index.js';
import { decodeCardinality, decodeRange, encodeCardinality, encodeRange } from './constraintTemplates.js';

// ── 服务端形状（只声明用到的字段）──

export interface ServerAttribute {
  name: string;
  type?: string;
  required?: boolean;
  description?: string;
  display_name?: string;
  is_primary_key?: boolean;
  enum_values?: string[];
  source_attribute_id?: string | null;
}

export interface ServerEntityType {
  name: string;
  display_name?: string;
  description?: string;
  aliases?: string[];
  attributes?: ServerAttribute[];
  // 下面几个画布**不建模**，但服务端保存时会从请求体里读（见 toServerSchema 的说明）
  status?: string;
  requirement?: string;
  source_object_id?: string | null;
  extraction_skills?: unknown;
}

export interface ServerRelationType {
  name: string;
  source: string;
  target: string;
  description?: string;
  display_name?: string;
  cardinality?: string;
  inverse_of?: string;
  source_types?: string[];
  target_types?: string[];
  status?: string;
  aliases?: string[];
  // join 键（P0-2）：服务端编译 schema 上的字段。画布**不建模**它们，
  // 只在保存时从基线原样回贴 —— 所以必须同时进 `RELATION_EXTRAS`。
  source_column?: string | null;
  target_column?: string | null;
  origin?: string | null;
  source_relation_id?: string | null;
}

/** 服务端 7 键形状（`_normalize_constraints` 白名单）。画布不建模的字段只是不读；畸形条目（无 name/target_code 的 demo stub）读侧跳过。 */
export interface ServerConstraint {
  name?: string;
  target_type?: string;
  target_code?: string;
  target_label?: string | null;
  constraint_type?: string;
  expression?: string;
  suggestion?: string;
}

/** 畸形载荷防御：`constraints` 数组里可能混进 null / 非对象元素（历史数据、手工写入）。
 *  读侧跳过、写侧丢弃——脏数据不炸页面（与后端 `_strip_legacy_stub` 同姿态）。 */
const isConstraintObject = (value: unknown): value is ServerConstraint =>
  value !== null && typeof value === 'object';

export interface ServerSchema {
  entity_types?: ServerEntityType[];
  relation_types?: ServerRelationType[];
  constraints?: ServerConstraint[];
  disambiguation?: { case_insensitive?: boolean; alias_merge?: boolean };
  schema_version?: number;
  /** 知识库级读取时才可能出现：`"abstract"` = 这一份是抽象 schema 的**投影**（不是真实行，保存已停用） */
  source_type?: string;
}

// ── 稳定 id ──

const entityId = (name: string): string => `e:${name}`;
const attributeId = (entityName: string, attributeName: string): string => `a:${entityName}.${attributeName}`;
const relationId = (name: string): string => `r:${name}`;
/** 约束在服务端没有独立 id，用「类型+目标」派生。同档同目标至多一条**不是**服务端保证的——它靠 name 列表内唯一 + 各写入方 name 均为 `{type}_{target_code}`（`.`→`_`）派生的约定（后端 `_derive_constraint_name`） */
export const serverConstraintId = (constraintType: string, targetCode: string): string =>
  `c:${constraintType}:${targetCode}`;

/** 画布改过的名字 → 服务端名字。id 前缀里带着加载时的原名，用它回填。 */
const originalName = (id: string): string => {
  const body = id.replace(/^[earc]:/, '');
  return body.includes(':') ? body.slice(body.indexOf(':') + 1) : body;
};

// ── 服务端 → 画布 ──

const ATTRIBUTE_TYPES: readonly string[] = ['string', 'text', 'number', 'integer', 'boolean', 'date', 'datetime', 'enum'];

function toAttribute(entityName: string, raw: ServerAttribute): Attribute {
  const type = (raw.type && ATTRIBUTE_TYPES.includes(raw.type) ? raw.type : 'string') as AttributeType;
  const attribute: Attribute = {
    id: attributeId(entityName, raw.name),
    name: raw.name,
    type,
    required: raw.required === true,
    primaryKey: raw.is_primary_key === true,
  };
  if (raw.description) attribute.description = raw.description;
  if (raw.enum_values?.length) attribute.enumValues = [...raw.enum_values];
  return attribute;
}

function toEntity(raw: ServerEntityType): EntityType {
  const entity: EntityType = {
    id: entityId(raw.name),
    name: raw.name,
    aliases: [...(raw.aliases ?? [])],
    attributes: (raw.attributes ?? []).map((attribute) => toAttribute(raw.name, attribute)),
  };
  if (raw.display_name) entity.displayName = raw.display_name;
  if (raw.description) entity.description = raw.description;
  return entity;
}

function toRelation(raw: ServerRelationType): RelationType {
  const relation: RelationType = {
    id: relationId(raw.name),
    name: raw.name,
    sourceEntityId: entityId(raw.source),
    targetEntityId: entityId(raw.target),
  };
  if (raw.description) relation.description = raw.description;
  if (raw.cardinality) relation.cardinality = raw.cardinality as Cardinality;
  if (raw.inverse_of) relation.inverseOf = raw.inverse_of;
  if (raw.source_types?.length) relation.sourceTypes = raw.source_types.map(entityId);
  if (raw.target_types?.length) relation.targetTypes = raw.target_types.map(entityId);
  return relation;
}

/** 画布可表达的 attribute 级档位（`relation_cardinality` 归 relation 级） */
const ATTRIBUTE_RULE_TYPES = ['required_attribute', 'unique_attribute', 'value_range', 'query_rule'] as const;

const isAttributeRuleType = (value: string): value is (typeof ATTRIBUTE_RULE_TYPES)[number] =>
  (ATTRIBUTE_RULE_TYPES as readonly string[]).includes(value);

/**
 * 服务端 7 键 → 画布约束。
 * 返回 undefined 的三种情形（都不进画布，写侧各有着落）：畸形（无 constraint_type/target_code；另有
 * 一条点位置防御分支 `dot <= 0 || dot === targetCode.length - 1`——成员校验已保证码形如 `Entity.attr`，
 * 实际不可达）、**不可表达**（未知档、target_type 错配、entity 级——写侧保真回贴）、目标不存在（写侧丢弃）。
 */
function toConstraint(
  raw: ServerConstraint,
  attributeCodes: Set<string>,
  relationNames: Set<string>,
): Constraint | undefined {
  const constraintType = (raw.constraint_type ?? '').trim();
  const targetCode = (raw.target_code ?? '').trim();
  const targetType = (raw.target_type ?? '').trim().toLowerCase();
  if (!constraintType || !targetCode) return undefined;
  const id = serverConstraintId(constraintType, targetCode);

  if (constraintType === 'relation_cardinality') {
    if (targetType !== 'relation' || !relationNames.has(targetCode)) return undefined;
    return {
      id, type: 'relation_cardinality', relationId: relationId(targetCode), severity: 'error',
      ...decodeCardinality(raw.expression ?? ''),
    };
  }

  if (!isAttributeRuleType(constraintType) || targetType !== 'attribute' || !attributeCodes.has(targetCode)) return undefined;
  const dot = targetCode.indexOf('.');
  if (dot <= 0 || dot === targetCode.length - 1) return undefined;
  const entity = targetCode.slice(0, dot);
  const attribute = targetCode.slice(dot + 1);
  const base = { id, entityId: entityId(entity), attributeId: attributeId(entity, attribute), severity: 'error' } as const;

  if (constraintType === 'value_range') {
    return { ...base, type: 'value_range', ...decodeRange(raw.expression ?? '') };
  }
  if (constraintType === 'query_rule') {
    return { ...base, type: 'query_rule', expression: raw.expression ?? '' };
  }
  return constraintType === 'required_attribute'
    ? { ...base, type: 'required_attribute' }
    : { ...base, type: 'unique_attribute' };
}

/** 服务端 schema → 画布文档。id 一律从名字派生，重复加载得到同一批 id。 */
export function toDocument(kbId: string, displayName: string, schema: ServerSchema): DomainDocument {
  const attributeCodes = new Set(
    (schema.entity_types ?? []).flatMap((item) => (item.attributes ?? []).map((attribute) => `${item.name}.${attribute.name}`)),
  );
  const relationNames = new Set((schema.relation_types ?? []).map((item) => item.name));

  const model: OntologyModel = {
    entities: (schema.entity_types ?? []).map(toEntity),
    relations: (schema.relation_types ?? []).map(toRelation),
    constraints: (schema.constraints ?? [])
      .filter(isConstraintObject)
      .map((item) => toConstraint(item, attributeCodes, relationNames))
      .filter((item): item is Constraint => item !== undefined),
    disambiguation: {
      caseInsensitive: schema.disambiguation?.case_insensitive !== false,
      aliasMerge: schema.disambiguation?.alias_merge !== false,
    },
  };
  // 坐标由 layout.ts 在加载后统一算，这里先给空——buildDocumentFromModel 会填占位
  const document = buildDocumentFromModel(model, { id: kbId, displayName });
  document.editor.nodePositions = {};
  return document;
}

// ── 画布 → 服务端 ──

function fromAttribute(raw: Attribute): ServerAttribute {
  const attribute: ServerAttribute = {
    name: raw.name,
    type: raw.type,
    required: raw.required,
    is_primary_key: raw.primaryKey,
  };
  if (raw.description) attribute.description = raw.description;
  if (raw.enumValues?.length) attribute.enum_values = [...raw.enumValues];
  return attribute;
}

/**
 * 画布**不建模**、但服务端保存时会从请求体里读的字段。
 *
 * 保存是**整份替换**，不透传就会被服务端按默认值覆盖：
 *   - `extraction_skills` → None，抽取技能链**静默跳过**（后端 `_build_prompt_schema`
 *     自己也在 warn 这件事）
 *   - `status` → `'active'`，把 Compile 页里**停用过的实体/关系重新启用**
 * 服务端新增这类字段时，这里要跟着加。
 */
const ENTITY_EXTRAS = ['status', 'requirement', 'source_object_id', 'extraction_skills'] as const;
const RELATION_EXTRAS = ['status', 'aliases', 'source_relation_id', 'source_column', 'target_column', 'origin'] as const;
const ATTRIBUTE_EXTRAS = ['display_name', 'source_attribute_id'] as const;

/** 从基线里挑出这些键——只挑基线里**存在**的，不凭空造字段。 */
function pickExtras(source: object | undefined, keys: readonly string[]): Record<string, unknown> {
  if (!source) return {};
  const out: Record<string, unknown> = {};
  for (const key of keys) {
    const value = (source as Record<string, unknown>)[key];
    if (value !== undefined) out[key] = value;
  }
  return out;
}

// ── 约束：写侧（匹配法，spec §五） ──

/** id 里带的加载名 → 当前名。改名跟随靠它（画布改名不动 id，见文件头）。 */
interface RenameMap {
  entity: Map<string, string>;
  attribute: Map<string, string>;
  relation: Map<string, string>;
}

function buildRenameMap(document: DomainDocument): RenameMap {
  const entity = new Map<string, string>();
  const attribute = new Map<string, string>();
  for (const item of document.model.entities) {
    const loadedName = originalName(item.id);
    entity.set(loadedName, item.name);
    const prefix = `a:${loadedName}.`;
    for (const attr of item.attributes) {
      const loadedAttr = attr.id.startsWith(prefix) ? attr.id.slice(prefix.length) : attr.name;
      attribute.set(`${loadedName}.${loadedAttr}`, `${item.name}.${attr.name}`);
    }
  }
  const relation = new Map<string, string>();
  for (const item of document.model.relations) relation.set(originalName(item.id), item.name);
  return { entity, attribute, relation };
}

/** 基线 target_code（加载名）→ 当前名；目标已不在画布 → undefined（该项丢弃）。 */
function mapBaselineTarget(map: RenameMap, targetType: string, code: string): string | undefined {
  if (targetType === 'entity') return map.entity.get(code);
  if (targetType === 'attribute') return map.attribute.get(code);
  if (targetType === 'relation') return map.relation.get(code);
  return undefined;
}

/** 画布可表达的合法条目（写侧「配对池」与「保真桶排除」共用此判据）。 */
function isExpressible(raw: ServerConstraint): boolean {
  const constraintType = (raw.constraint_type ?? '').trim();
  const targetType = (raw.target_type ?? '').trim().toLowerCase();
  if (constraintType === 'relation_cardinality') return targetType === 'relation';
  return isAttributeRuleType(constraintType) && targetType === 'attribute';
}

/** 逐字复刻后端 `_derive_constraint_name`（ontology_compiler.py:1508）。 */
const deriveConstraintName = (constraintType: string, targetCode: string): string =>
  `${constraintType}_${targetCode.replace(/\./g, '_')}`;

/**
 * 画布约束 + baseline → 服务端 `constraints` 数组。
 *
 * 匹配法：baseline 的 target_code 先经「加载名→当前名」映射翻到当前，再按
 * `(constraint_type, target_code, target_type)` 与画布约束配对——配上的沿用 baseline 的
 * `name`/`target_label`/`suggestion`（人工命名 round-trip 稳定），配不上的按规则重派生。
 * 保真桶只接画布**表达不了**的条目；可表达的一律以画布为权威（没配上的=用户删了）。
 */
function buildConstraints(document: DomainDocument, baseline?: ServerSchema): ServerConstraint[] {
  const entityById = new Map(document.model.entities.map((item) => [item.id, item]));
  const relationNameById = new Map(document.model.relations.map((item) => [item.id, item.name]));
  const rename = buildRenameMap(document);

  // 基线上的合法可表达条目：按 (类型|target_type|映射后 target_code) 建索引，供画布条目配对
  const baselineByKey = new Map<string, ServerConstraint>();
  for (const raw of baseline?.constraints ?? []) {
    if (!isConstraintObject(raw)) continue;
    const name = (raw.name ?? '').trim();
    const code = (raw.target_code ?? '').trim();
    const targetType = (raw.target_type ?? '').trim().toLowerCase();
    if (!name || !code || !isExpressible(raw)) continue;
    const mapped = mapBaselineTarget(rename, targetType, code);
    if (mapped === undefined) continue;
    const key = `${(raw.constraint_type ?? '').trim()}|${targetType}|${mapped}`;
    // 刻意保留首条（first-wins）：服务端只保 name 唯一、不保 (type,target) 唯一；同键重复需外部
    // 绕过命名约定才能构造，届时任何一条都会触发 name 唯一 400 —— 此处置首条即可
    if (!baselineByKey.has(key)) baselineByKey.set(key, raw);
  }

  const fromCanvas: ServerConstraint[] = [];
  for (const constraint of document.model.constraints) {
    if (constraint.type === 'relation_cardinality') {
      const relationName = relationNameById.get(constraint.relationId);
      if (relationName === undefined) continue; // 悬空 → 丢弃（发了必 400）
      const matched = baselineByKey.get(`relation_cardinality|relation|${relationName}`);
      fromCanvas.push({
        name: matched?.name ?? deriveConstraintName(constraint.type, relationName),
        target_type: 'relation',
        target_code: relationName,
        target_label: matched ? (matched.target_label ?? null) : relationName,
        constraint_type: constraint.type,
        expression: encodeCardinality(constraint.min, constraint.max),
        suggestion: matched?.suggestion ?? '',
      });
      continue;
    }
    const entity = entityById.get(constraint.entityId);
    const attribute = entity?.attributes.find((item) => item.id === constraint.attributeId);
    if (!entity || !attribute) continue; // 悬空 → 丢弃
    const code = `${entity.name}.${attribute.name}`;
    const matched = baselineByKey.get(`${constraint.type}|attribute|${code}`);
    const expression =
      constraint.type === 'query_rule' ? constraint.expression
      : constraint.type === 'value_range' ? encodeRange(constraint.min, constraint.max)
      : (matched?.expression ?? '');
    fromCanvas.push({
      name: matched?.name ?? deriveConstraintName(constraint.type, code),
      target_type: 'attribute',
      target_code: code,
      target_label: matched ? (matched.target_label ?? null) : attribute.name,
      constraint_type: constraint.type,
      expression,
      suggestion: matched?.suggestion ?? '',
    });
  }

  // 保真桶：画布表达不了的条目原样回贴（改名跟随、目标没了丢、畸形丢）
  const preserved: ServerConstraint[] = [];
  for (const raw of baseline?.constraints ?? []) {
    if (!isConstraintObject(raw)) continue;
    const name = (raw.name ?? '').trim();
    const code = (raw.target_code ?? '').trim();
    const targetType = (raw.target_type ?? '').trim().toLowerCase();
    if (!name || !code) continue;                                         // 畸形（含 demo stub）→ 丢
    if (isExpressible(raw)) continue;                                     // 画布权威，不保真
    if (!['entity', 'attribute', 'relation'].includes(targetType)) continue; // 非法 target_type → 丢
    const mapped = mapBaselineTarget(rename, targetType, code);
    if (mapped === undefined) continue;                                   // 目标被删 → 丢
    preserved.push({
      name,
      target_type: targetType,
      target_code: mapped,
      target_label: raw.target_label ?? null,
      // 空白/空串归 'custom'：后端 `_normalize_constraints` 会把空白串 strip 成 ''，而 DTO
      //（CompiledConstraintInput.constraint_type）有 min_length=1 —— 原样回发会 422、整次保存失败
      constraint_type: (raw.constraint_type ?? '').trim() || 'custom',
      expression: raw.expression ?? '',
      suggestion: raw.suggestion ?? '',
    });
  }

  return [...fromCanvas, ...preserved];
}

/**
 * 画布文档 → 服务端 schema。
 *
 * ⚠️ 引用按**当前名字**解算，不是 id 里带的名字——所以画布上把 `Asset` 改名成
 * `资产` 之后，指向它的关系端点会一起跟着变（这正是期望行为：改名不该扯断关系）。
 *
 * `baseline` 是本次加载时服务端给的原文，用来回贴画布不建模的字段（见 ENTITY_EXTRAS）。
 * **建模中的字段一律以画布为准**——所以它们写在展开之后，清空 description 这类
 * 「省略即清空」的语义才不会被基线打回来。
 */
export function toServerSchema(document: DomainDocument, baseline?: ServerSchema): ServerSchema {
  const nameById = new Map(document.model.entities.map((entity) => [entity.id, entity.name]));
  const baselineEntities = new Map((baseline?.entity_types ?? []).map((item) => [item.name, item]));
  const baselineRelations = new Map((baseline?.relation_types ?? []).map((item) => [item.name, item]));

  const entity_types = document.model.entities.map((entity) => {
    // id 里带的是**加载时**的名字——画布上改名后它仍是基线的键
    const loadedName = originalName(entity.id);
    const before = baselineEntities.get(loadedName);
    const baselineAttrs = new Map((before?.attributes ?? []).map((item) => [item.name, item]));
    const attrPrefix = `a:${loadedName}.`;

    return {
      ...pickExtras(before, ENTITY_EXTRAS),
      name: entity.name,
      ...(entity.displayName ? { display_name: entity.displayName } : {}),
      ...(entity.description ? { description: entity.description } : {}),
      aliases: entity.aliases,
      attributes: entity.attributes.map((attribute) => ({
        ...pickExtras(
          baselineAttrs.get(attribute.id.startsWith(attrPrefix) ? attribute.id.slice(attrPrefix.length) : attribute.name),
          ATTRIBUTE_EXTRAS,
        ),
        ...fromAttribute(attribute),
      })),
    };
  });

  const relation_types = document.model.relations.map((relation) => {
    const before = baselineRelations.get(originalName(relation.id));
    const source = nameById.get(relation.sourceEntityId) ?? originalName(relation.sourceEntityId);
    const target = nameById.get(relation.targetEntityId) ?? originalName(relation.targetEntityId);
    const extras = pickExtras(before, RELATION_EXTRAS);
    // ⚠️ 判据必须比**加载时**的名字（id 里带的那个），不是画布上当前的名字：
    // 给端点实体改名是一等操作（id 不变），而 join 键描述的是**物理表上的列**，
    // 改名不改表 —— 拿当前名字比会把「改名」误判成「换了端点」，一次保存就把键清光。
    const endpointsChanged =
      before != null && (originalName(relation.sourceEntityId) !== before.source
                      || originalName(relation.targetEntityId) !== before.target);
    // 端点**确实**换到了别的实体（`e:Dept`，或画布上新建实体——`newId()` 是 uuid、
    // `originalName` 回不出基线名）才清：那时 `before` 上的 join 键属于**旧端点** —— 回贴到
    // 新关系上会被 ② 侧的「对端列不存在 → 回落主键」洗成一条没人声明过的等值条件。
    // 只清这三个键；`status` / `aliases` / `source_relation_id` 与端点无关，照旧回贴。
    if (endpointsChanged) {
      delete extras.source_column;
      delete extras.target_column;
      delete extras.origin;
    }
    return {
      ...extras,
      name: relation.name,
      source,
      target,
      ...(relation.description ? { description: relation.description } : {}),
      ...(relation.cardinality ? { cardinality: relation.cardinality } : {}),
      ...(relation.inverseOf ? { inverse_of: relation.inverseOf } : {}),
      // 多态端点：若集合与单值相同（就一个元素）则不写——服务端的「单值回落」靠它
      ...(relation.sourceTypes && relation.sourceTypes.length > 1
        ? { source_types: relation.sourceTypes.map((id) => nameById.get(id) ?? originalName(id)) } : {}),
      ...(relation.targetTypes && relation.targetTypes.length > 1
        ? { target_types: relation.targetTypes.map((id) => nameById.get(id) ?? originalName(id)) } : {}),
    };
  });

  return {
    entity_types,
    relation_types,
    // 约束走**匹配法**（见 buildConstraints 的说明）：配上的沿用 baseline 的
    // name/target_label/suggestion（人工命名 round-trip 稳定）、配不上的重派生；
    // 画布表达不了的基线条目保真回贴；畸形与目标已删的丢弃。
    // 恒发（非空或 `[]`）——`[]` 的语义 = 显式空集合（删光/本就为空），合法。
    constraints: buildConstraints(document, baseline),
    disambiguation: {
      case_insensitive: document.model.disambiguation.caseInsensitive,
      alias_merge: document.model.disambiguation.aliasMerge,
    },
    schema_version: document.revision,
  };
}
