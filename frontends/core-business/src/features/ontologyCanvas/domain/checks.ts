import { DomainIdSchema } from './schemas.js';
import type { Attribute, Constraint, DomainDocument, EntityType, ExportIssue } from './types.js';

const issue = (code: string, level: 'mustFix' | 'confirm', kind: ExportIssue['target'] extends infer _ ? 'domain' | 'entity' | 'attribute' | 'relation' | 'constraint' : never, id: string, field?: string, params?: Record<string, string | number>): ExportIssue => ({
  code,
  level,
  target: { kind, id, ...(field ? { field } : {}) },
  ...(params ? { params } : {}),
});

const duplicateValues = (values: string[]) => values.filter((value, index) => values.findIndex((item) => item.toLowerCase() === value.toLowerCase()) !== index);
const recommendedAttributeName = /^[a-z][a-z0-9_]*$/;
const CJK = /[一-鿿]/;

/**
 * 名字里是否含有能进 IRI 的字符(CJK 或 ASCII 字母数字)。
 *
 * 这是 IRI 铸造层(rdf/iri.ts `normalizeLocalName`)回落成通用 `Entity` 的**唯一**条件 ——
 * 真回落了,所有这类实体会铸到同一个 IRI 上互相覆盖(semantica 12d7412 的塌缩成因)。
 * 大小写与分隔符**不**在这条检查里设卡:铸造层两种约定都收(`sec_company`→`SecCompany`、
 * `Person`→`Person`、`变压器 型号`→`变压器型号`),源名是哪个入口产的就该是哪种写法 ——
 * 归纳链路(induction/prompt.ts)一律 snake_case,AI 提案链路(proposal.test.ts、prompt.ts)是 PascalCase。
 * 原先的 `/^[A-Z][A-Za-z0-9]*$/` 只认后者,于是每一条摘出来的 snake_case 实体名都在导出时被误报。
 */
const hasIriCharacters = (name: string) => CJK.test(name) || /[a-zA-Z0-9]/.test(name);
const compatibleRangeTypes = new Set(['number', 'integer', 'date', 'datetime']);

/**
 * 阻断优先。检查是分环遍历的(域 → 实体 → 属性 → 关系 → 约束),而「必须修复」多产在靠后的环、
 * 「导出前确认」多产在靠前的环 —— 不排序时导出弹窗会把 mustFix 埋在几十条质量提示下面,
 * 用户看到的是「一片黄」却找不到真正拦着导出的那几条。
 * `Array.prototype.sort` 是稳定排序 ⇒ 同级保持原有相对顺序(仍按遍历序读得通)。
 */
const SEVERITY_RANK: Record<ExportIssue['level'], number> = { mustFix: 0, confirm: 1 };

function findAttribute(entity: EntityType | undefined, id: string): Attribute | undefined {
  return entity?.attributes.find((attribute) => attribute.id === id);
}

function checkConstraint(document: DomainDocument, constraint: Constraint, entityById: Map<string, EntityType>, relationIds: Set<string>): ExportIssue[] {
  if (constraint.type === 'relation_cardinality') {
    const result: ExportIssue[] = [];
    if (!relationIds.has(constraint.relationId)) result.push(issue('checks.constraint.missingRelation', 'mustFix', 'constraint', constraint.id));
    if (constraint.min < 0 || (constraint.max !== 'unbounded' && (constraint.max < 0 || constraint.max < constraint.min))) result.push(issue('checks.constraint.invalidCardinality', 'mustFix', 'constraint', constraint.id));
    return result;
  }
  const entity = entityById.get(constraint.entityId);
  const attribute = findAttribute(entity, constraint.attributeId);
  if (!entity || !attribute) return [issue('checks.constraint.missingAttribute', 'mustFix', 'constraint', constraint.id)];
  if (constraint.type === 'value_range') {
    const result: ExportIssue[] = [];
    if (!compatibleRangeTypes.has(attribute.type)) result.push(issue('checks.constraint.incompatibleRange', 'mustFix', 'constraint', constraint.id));
    if (constraint.min !== undefined && constraint.max !== undefined && constraint.min > constraint.max) result.push(issue('checks.constraint.invalidRange', 'mustFix', 'constraint', constraint.id));
    return result;
  }
  return [];
}

export function checkForExport(document: DomainDocument): ExportIssue[] {
  const issues: ExportIssue[] = [];
  if (!DomainIdSchema.safeParse(document.domain.id).success) issues.push(issue('checks.domain.invalidId', 'mustFix', 'domain', document.domain.id, 'id'));
  if (document.schemaVersion !== 1) issues.push(issue('checks.domain.invalidVersion', 'mustFix', 'domain', document.domain.id));
  if (document.model.entities.length === 0) issues.push(issue('checks.schema.empty', 'confirm', 'domain', document.domain.id));

  const entityById = new Map(document.model.entities.map((entity) => [entity.id, entity]));
  const relationIds = new Set(document.model.relations.map((relation) => relation.id));
  const entityNames: string[] = [];
  const aliases = new Map<string, string>();

  for (const entity of document.model.entities) {
    if (!entity.name.trim()) issues.push(issue('checks.entity.nameRequired', 'mustFix', 'entity', entity.id, 'name'));
    if (entityNames.includes(entity.name.toLowerCase())) issues.push(issue('checks.entity.nameDuplicate', 'mustFix', 'entity', entity.id, 'name', { name: entity.name }));
    entityNames.push(entity.name.toLowerCase());
    if (!hasIriCharacters(entity.name)) issues.push(issue('checks.entity.nameStyle', 'confirm', 'entity', entity.id, 'name'));
    if (!entity.attributes.length && !entity.description && !entity.aliases.length) issues.push(issue('checks.entity.incomplete', 'confirm', 'entity', entity.id));

    const attributeNames: string[] = [];
    for (const attribute of entity.attributes) {
      if (!attribute.name.trim()) issues.push(issue('checks.attribute.nameRequired', 'mustFix', 'attribute', attribute.id, 'name'));
      if (attributeNames.includes(attribute.name.toLowerCase())) issues.push(issue('checks.attribute.nameDuplicate', 'mustFix', 'attribute', attribute.id, 'name', { name: attribute.name }));
      attributeNames.push(attribute.name.toLowerCase());
      if (!recommendedAttributeName.test(attribute.name)) issues.push(issue('checks.attribute.nameStyle', 'confirm', 'attribute', attribute.id, 'name'));
      if (attribute.primaryKey && !attribute.required) issues.push(issue('checks.attribute.primaryKeyRequired', 'mustFix', 'attribute', attribute.id, 'required'));
      if (attribute.type === 'enum') {
        if (!(attribute.enumValues?.length)) issues.push(issue('checks.attribute.emptyEnum', 'confirm', 'attribute', attribute.id, 'enumValues'));
        if (duplicateValues(attribute.enumValues ?? []).length) issues.push(issue('checks.attribute.duplicateEnum', 'mustFix', 'attribute', attribute.id, 'enumValues'));
      }
    }
    for (const alias of entity.aliases.filter(Boolean)) {
      const key = alias.toLowerCase();
      if (aliases.has(key) && aliases.get(key) !== entity.id) issues.push(issue('checks.entity.aliasConflict', 'confirm', 'entity', entity.id, 'aliases', { alias }));
      aliases.set(key, entity.id);
    }
  }

  const relationKeys: string[] = [];
  const connected = new Set<string>();
  for (const relation of document.model.relations) {
    if (!relation.name.trim()) issues.push(issue('checks.relation.nameRequired', 'mustFix', 'relation', relation.id, 'name'));
    // 关系名与实体名/属性名同口径:**按裸名(大小写不敏感)去重**,端点不参与。
    // 关系名是全局的:OWL 属性 IRI 只由名字铸造(RDF 属性无「属于哪个实体」的容器),
    // 同名关系会被铸成同一个属性,发射时往上写第二条 rdfs:domain/range —— 那是**交集**语义;
    // 下游 YAML 转换/合并也一律按名字索引,同名关系会被静默并成一条。
    const key = relation.name.toLowerCase();
    if (relationKeys.includes(key)) issues.push(issue('checks.relation.duplicate', 'mustFix', 'relation', relation.id));
    relationKeys.push(key);
    if (!entityById.has(relation.sourceEntityId) || !entityById.has(relation.targetEntityId)) issues.push(issue('checks.relation.missingEntity', 'mustFix', 'relation', relation.id));
    // 多态端点:知识库侧编译器要求「集合必须包含单值」且集合内引用存在的实体
    // (ontology_compiler.py 同款不变量)——越界会让整份 TBox 在知识库侧编译失败,故导出前 mustFix 拦下
    for (const [field, ids, single] of [['sourceTypes', relation.sourceTypes, relation.sourceEntityId], ['targetTypes', relation.targetTypes, relation.targetEntityId]] as const) {
      if (ids === undefined || ids.length === 0) continue;
      if (!ids.includes(single)) issues.push(issue(field === 'sourceTypes' ? 'checks.relation.sourceTypesMissingSingle' : 'checks.relation.targetTypesMissingSingle', 'mustFix', 'relation', relation.id, field));
      if (ids.some((id) => !entityById.has(id))) issues.push(issue('checks.relation.typesMissingEntity', 'mustFix', 'relation', relation.id, field));
    }
    connected.add(relation.sourceEntityId);
    connected.add(relation.targetEntityId);
    if (!relation.description) issues.push(issue('checks.relation.descriptionMissing', 'confirm', 'relation', relation.id, 'description'));
  }

  for (const entity of document.model.entities) {
    if (document.model.entities.length > 1 && !connected.has(entity.id)) issues.push(issue('checks.entity.isolated', 'confirm', 'entity', entity.id));
  }
  for (const constraint of document.model.constraints) issues.push(...checkConstraint(document, constraint, entityById, relationIds));

  const constraintKeys = new Set<string>();
  for (const constraint of document.model.constraints) {
    const key = constraint.type === 'relation_cardinality' ? `${constraint.type}|${constraint.relationId}` : `${constraint.type}|${constraint.attributeId}`;
    if (constraintKeys.has(key)) issues.push(issue('checks.constraint.duplicate', 'mustFix', 'constraint', constraint.id));
    constraintKeys.add(key);
  }

  // 原实现在这里跑一遍 YAML 序列化做兜底（序列化失败即 mustFix）。
  // 本移植不带 YAML 序列化层：落库走 Jonex 的 JSON API，YAML 由服务端负责导出，
  // 画布不产生 YAML。保留这条会依赖一个我们并不持有的能力。
  // 真正拦住导出的判据（重名、悬空引用、PK 必填、约束目标存在）都在上面，覆盖面不受影响。
  return issues.sort((a, b) => SEVERITY_RANK[a.level] - SEVERITY_RANK[b.level]);
}
