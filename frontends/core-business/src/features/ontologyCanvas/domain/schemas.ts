import { z } from 'zod';

// domain.id 直接用知识库 id（`toDocument` 原样传入），而 KB id 是无前缀 UUID 十六进制，
// 首字符可能是数字（如 `1e2797b4e0f54c99b2ea9ced3ecb61b4`）——不能要求以字母开头。
export const DomainIdSchema = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{1,47}$/);
export const AttributeTypeSchema = z.enum(['string', 'text', 'number', 'integer', 'boolean', 'date', 'datetime', 'enum']);
export const SeveritySchema = z.enum(['error', 'warning']);
/** 关系基数(与知识库侧 TBox relation_types[].cardinality 同词汇) */
export const CardinalitySchema = z.enum(['one_to_one', 'one_to_many', 'many_to_many', 'many_to_one', 'custom']);

/** TBox 技能声明:已知字段校验、未知子键保留(passthrough)—— 编辑器只透传,不解释 */
export const ExtractionSkillRefSchema = z.object({
  skill: z.string(),
  description: z.string().optional(),
}).passthrough();
export const ExtractionSkillsSchema = z.object({
  refine: z.array(ExtractionSkillRefSchema).optional(),
  constrain: z.array(ExtractionSkillRefSchema).optional(),
}).passthrough();

/** 属性来源（仅抽象 schema 画布使用；编译画布不设此字段）。 */
export const AttributeSourceSchema = z.object({
  dataSourceId: z.string(),
  entityName: z.string(),
  attributeName: z.string(),
  role: z.string().optional(),
  // P1-5 语义槽：`buildDocumentFromModel` 会过 `DomainDocumentSchema.parse`，
  // 未登记的键会被 zod 静默剥掉——四键必须在这里显式登记，round-trip 才不会丢。
  filter: z.string().optional(),
  aggregation: z.string().optional(),
  grain: z.string().optional(),
  enabled: z.boolean().optional(),
  stale: z.boolean().optional(),
  staleReason: z.string().nullable().optional(),
});

/** 实体落点（仅抽象 schema 画布使用；编译画布不设此字段）。 */
export const EntityLandingSchema = z.object({
  dataSourceId: z.string(),
  entityName: z.string(),
  included: z.boolean(),
  sourceName: z.string().nullable().optional(),
  stale: z.boolean().optional(),
  staleReason: z.string().nullable().optional(),
});

/** 关系物理背书（仅抽象 schema 画布使用；编译画布不设此字段）。 */
export const RelationBackingSchema = z.object({
  dataSourceId: z.string(),
  relationName: z.string(),
  sourceColumn: z.string().nullable().optional(),
  targetColumn: z.string().nullable().optional(),
  origin: z.string().nullable().optional(),
  stale: z.boolean().optional(),
  staleReason: z.string().nullable().optional(),
});

export const AttributeSchema = z.object({
  id: z.string().min(1),
  name: z.string(),
  type: AttributeTypeSchema,
  required: z.boolean(),
  primaryKey: z.boolean(),
  description: z.string().optional(),
  enumValues: z.array(z.string()).optional(),
  displayName: z.string().optional(),
  sources: z.array(AttributeSourceSchema).optional(),
});

export const EntityTypeSchema = z.object({
  id: z.string().min(1),
  name: z.string(),
  displayName: z.string().optional(),
  aliases: z.array(z.string()),
  description: z.string().optional(),
  attributes: z.array(AttributeSchema),
  extractionSkills: ExtractionSkillsSchema.optional(),
  landings: z.array(EntityLandingSchema).optional(),
});

export const RelationTypeSchema = z.object({
  id: z.string().min(1),
  name: z.string(),
  sourceEntityId: z.string().min(1),
  targetEntityId: z.string().min(1),
  description: z.string().optional(),
  // 多态端点:内部存实体 id(导入时把 TBox 的类型名解析成 id);空数组/缺省 = 单值回落
  sourceTypes: z.array(z.string().min(1)).optional(),
  targetTypes: z.array(z.string().min(1)).optional(),
  inverseOf: z.string().optional(),
  cardinality: CardinalitySchema.optional(),
  backings: z.array(RelationBackingSchema).optional(),
});

const ConstraintBaseSchema = z.object({ id: z.string().min(1), severity: SeveritySchema });
export const ConstraintSchema = z.discriminatedUnion('type', [
  ConstraintBaseSchema.extend({
    type: z.literal('required_attribute'),
    entityId: z.string().min(1),
    attributeId: z.string().min(1),
  }),
  ConstraintBaseSchema.extend({
    type: z.literal('unique_attribute'),
    entityId: z.string().min(1),
    attributeId: z.string().min(1),
  }),
  ConstraintBaseSchema.extend({
    type: z.literal('value_range'),
    entityId: z.string().min(1),
    attributeId: z.string().min(1),
    min: z.union([z.number(), z.string()]).optional(),
    max: z.union([z.number(), z.string()]).optional(),
  }),
  ConstraintBaseSchema.extend({
    type: z.literal('relation_cardinality'),
    relationId: z.string().min(1),
    min: z.number().int(),
    max: z.union([z.number().int(), z.literal('unbounded')]),
  }),
  ConstraintBaseSchema.extend({
    type: z.literal('query_rule'),
    entityId: z.string().min(1),
    attributeId: z.string().min(1),
    expression: z.string(),
  }),
]);

export const DomainDocumentSchema = z.object({
  storageVersion: z.literal(1),
  schemaVersion: z.literal(1),
  revision: z.number().int().nonnegative(),
  domain: z.object({
    id: DomainIdSchema,
    displayName: z.string().min(1),
    description: z.string().optional(),
  }),
  model: z.object({
    entities: z.array(EntityTypeSchema),
    relations: z.array(RelationTypeSchema),
    constraints: z.array(ConstraintSchema),
    disambiguation: z.object({ caseInsensitive: z.boolean(), aliasMerge: z.boolean() }),
  }),
  editor: z.object({
    nodePositions: z.record(z.string(), z.object({ x: z.number(), y: z.number() })),
    viewport: z.object({ x: z.number(), y: z.number(), zoom: z.number().min(0.3).max(2) }),
  }),
  savedAt: z.string(),
});

export const DomainSummarySchema = z.object({
  id: DomainIdSchema,
  displayName: z.string().min(1),
  description: z.string().optional(),
  schemaVersion: z.literal(1),
  entityCount: z.number().int().nonnegative(),
  relationCount: z.number().int().nonnegative(),
  ruleCount: z.number().int().nonnegative(),
  updatedAt: z.string(),
  revision: z.number().int().nonnegative(),
});
