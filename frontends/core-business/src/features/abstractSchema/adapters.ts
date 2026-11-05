/**
 * 抽象 schema v2 ⇄ 画布 DomainDocument 的适配层（纯函数）。
 *
 * 与 `ontologyCanvas/adapters.ts`（编译 schema）分开的理由：那边按名字回填
 * 服务端 extras、带 join 键白名单等**编译语义**；这边是抽象语义（落点/背书），
 * 混在一起会让两条路互相牵制。id 派生规则一致（`e:`/`r:`/`a:` 前缀 + 名字），
 * 因为复用同一批画布组件（ModelCanvas / PropertiesPanel）。
 */
import type {
  Attribute, AttributeSource, AttributeType, Cardinality, DomainDocument, EntityLanding, EntityType,
  RelationType,
} from '../ontologyCanvas/domain/index.js';
import { buildDocumentFromModel } from '../ontologyCanvas/domain/index.js';
import type {
  AbstractAttributeSource, AbstractBacking, AbstractLanding, AbstractSchemaBody,
} from '@/api/abstractSchema';

const ATTRIBUTE_TYPES: readonly string[] = ['string', 'text', 'number', 'integer', 'boolean', 'date', 'datetime', 'enum'];

/** 与画布 `Cardinality`（domain/types.ts）及 `CardinalitySchema`（domain/schemas.ts）同一词汇表。 */
const CARDINALITIES: readonly string[] = ['one_to_one', 'one_to_many', 'many_to_many', 'many_to_one', 'custom'];

const entityId = (name: string): string => `e:${name}`;
const attributeId = (entityName: string, attributeName: string): string => `a:${entityName}.${attributeName}`;
const relationId = (name: string): string => `r:${name}`;

export function toDocument(kbId: string, body: AbstractSchemaBody): DomainDocument {
  const model = {
    entities: body.entities.map((raw): EntityType => {
      const entity: EntityType = {
        id: entityId(raw.name),
        name: raw.name,
        aliases: [...(raw.aliases ?? [])],
        attributes: (raw.attributes ?? []).map((a): Attribute => {
          const attribute: Attribute = {
            id: attributeId(raw.name, a.name),
            name: a.name,
            type: (ATTRIBUTE_TYPES.includes(a.type) ? a.type : 'string') as AttributeType,
            required: a.required === true,
            primaryKey: a.primaryKey === true,
          };
          if (a.description) attribute.description = a.description;
          if (a.enumValues?.length) attribute.enumValues = [...a.enumValues];
          if (a.displayName) attribute.displayName = a.displayName;
          if (a.sources?.length) {
            attribute.sources = a.sources.map((s): AttributeSource => ({
              dataSourceId: s.dataSourceId,
              entityName: s.entityName,
              attributeName: s.attributeName,
              ...(s.role != null ? { role: s.role } : {}),
              // P1-5 语义槽：四键原样带回（只在有值时写，缺键不物化）。
              ...(s.filter ? { filter: s.filter } : {}),
              ...(s.aggregation ? { aggregation: s.aggregation } : {}),
              ...(s.grain ? { grain: s.grain } : {}),
              ...(typeof s.enabled === 'boolean' ? { enabled: s.enabled } : {}),
              stale: Boolean(s.stale),
              ...(s.staleReason != null ? { staleReason: s.staleReason } : {}),
            }));
          }
          return attribute;
        }),
        landings: (raw.sources ?? []).map((s): EntityLanding => ({
          dataSourceId: s.dataSourceId,
          entityName: s.entityName,
          included: s.included !== false,
          sourceName: s.sourceName ?? null,
          stale: s.stale,
          staleReason: s.staleReason ?? null,
        })),
      };
      if (raw.displayName) entity.displayName = raw.displayName;
      if (raw.description) entity.description = raw.description;
      return entity;
    }),
    relations: body.relations.map((raw): RelationType => {
      const relation: RelationType = {
        id: relationId(raw.name),
        name: raw.name,
        sourceEntityId: entityId(raw.source),
        targetEntityId: entityId(raw.target),
        backings: (raw.backings ?? []).map((b) => ({
          dataSourceId: b.dataSourceId,
          relationName: b.relationName,
          sourceColumn: b.sourceColumn ?? null,
          targetColumn: b.targetColumn ?? null,
          origin: b.origin ?? null,
          stale: b.stale,
          staleReason: b.staleReason ?? null,
        })),
      };
      // 基数按**白名单**收窄，不裸 cast：上游（TBox / 老数据）可能给出词汇表外的写法，
      // 原样带进文档会让 `buildDocumentFromModel` 里的 `DomainDocumentSchema.parse` 当场
      // throw —— 那是整页加载失败，代价远大于少一个基数标签。认不出的值 = 当作没声明
      // （字段整个不带，而不是回落成某个默认基数：猜错语义比留白更糟）。
      if (raw.cardinality && CARDINALITIES.includes(raw.cardinality)) relation.cardinality = raw.cardinality as Cardinality;
      if (raw.description) relation.description = raw.description;
      if (raw.inverseOf) relation.inverseOf = raw.inverseOf;
      if (raw.sourceTypes?.length) relation.sourceTypes = raw.sourceTypes.map(entityId);
      if (raw.targetTypes?.length) relation.targetTypes = raw.targetTypes.map(entityId);
      return relation;
    }),
    constraints: [],
    disambiguation: { caseInsensitive: true, aliasMerge: true },
  };
  const document = buildDocumentFromModel(model as never, { id: kbId, displayName: kbId });
  document.editor.nodePositions = {};
  return document;
}

export function toServerBody(document: DomainDocument): AbstractSchemaBody {
  const nameById = new Map(document.model.entities.map((entity) => [entity.id, entity.name]));
  const endpointNames = (ids?: string[]): string[] =>
    (ids ?? []).map((id) => nameById.get(id)).filter((n): n is string => Boolean(n));
  return {
    entities: document.model.entities.map((entity) => ({
      name: entity.name,
      displayName: entity.displayName,
      description: entity.description ?? '',
      aliases: [...entity.aliases],
      // 可空可选键**只在有值时发**（`null` 与「没这个键」对服务端同义，但带上空键
      // 会让往返断言（toEqual 不忽略 null）与下游 diff 多出噪声）。
      sources: (entity.landings ?? []).map((s) => ({
        dataSourceId: s.dataSourceId,
        entityName: s.entityName,
        included: s.included !== false,
        ...(s.sourceName != null ? { sourceName: s.sourceName } : {}),
        stale: Boolean(s.stale),
        // 画布侧 staleReason 是自由字符串（见 domain/types.ts 的说明），服务端要的是字面量联合 —— 在边界收窄
        ...(s.staleReason != null ? { staleReason: s.staleReason as AbstractLanding['staleReason'] } : {}),
      })),
      attributes: entity.attributes.map((a) => ({
        name: a.name,
        displayName: a.displayName,
        description: a.description,
        type: a.type,
        primaryKey: a.primaryKey,
        required: a.required,
        enumValues: a.enumValues,
        sources: (a.sources ?? []).map((s) => ({
          dataSourceId: s.dataSourceId,
          entityName: s.entityName,
          attributeName: s.attributeName,
          ...(s.role != null ? { role: s.role } : {}),
          // P1-5 语义槽：与读侧同「有值才写」纪律；键名在 saveAbstractSchema 那层
          // 原样落到 wire（三个单词在 snake/camel 下同形）。
          ...(s.filter ? { filter: s.filter } : {}),
          ...(s.aggregation ? { aggregation: s.aggregation } : {}),
          ...(s.grain ? { grain: s.grain } : {}),
          ...(typeof s.enabled === 'boolean' ? { enabled: s.enabled } : {}),
          stale: Boolean(s.stale),
          ...(s.staleReason != null ? { staleReason: s.staleReason as AbstractAttributeSource['staleReason'] } : {}),
        })),
      })),
    })),
    relations: document.model.relations.map((relation) => {
      const sourceTypes = endpointNames(relation.sourceTypes);
      const targetTypes = endpointNames(relation.targetTypes);
      return {
        name: relation.name,
        source: nameById.get(relation.sourceEntityId) ?? '',
        target: nameById.get(relation.targetEntityId) ?? '',
        cardinality: relation.cardinality,
        description: relation.description ?? '',
        // 多态端点：单值不写（「单值=未设置」与编译画布 `> 1` 口径一致；后端
        // normalize 也会把 ≤1 洗掉，两处同口径是防 UI 保存路径丢键的经典坑）。
        // 键名是**域形状**（camelCase）——本函数的出参直接喂 `saveAbstractSchema`，
        // snake_case 转换在那层做；这里写 snake 会被静默丢弃。
        ...(relation.inverseOf ? { inverseOf: relation.inverseOf } : {}),
        ...(sourceTypes.length > 1 ? { sourceTypes } : {}),
        ...(targetTypes.length > 1 ? { targetTypes } : {}),
        backings: (relation.backings ?? []).map((b) => ({
          dataSourceId: b.dataSourceId,
          relationName: b.relationName,
          ...(b.sourceColumn != null ? { sourceColumn: b.sourceColumn } : {}),
          ...(b.targetColumn != null ? { targetColumn: b.targetColumn } : {}),
          ...(b.origin != null ? { origin: b.origin } : {}),
          stale: Boolean(b.stale),
          ...(b.staleReason != null ? { staleReason: b.staleReason as AbstractBacking['staleReason'] } : {}),
        })),
      };
    }),
  };
}
