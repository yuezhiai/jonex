/**
 * 多源视图的纯模型层：把「每个源的 ds 级编译 schema」+「抽象 schema」装配成
 * 一张合并图。**不同源同名实体不自动并**——节点 id 一律命名空间化为
 * `${sourceId}:${实体名}`，同源关系重映射到命名空间 id；跨源只通过
 * 概念连线表达（且只连**不同源**的落点）。
 *
 * 这一层不含 React / 网络，纯函数，可单测（与 adapters.ts 同款拆法）。
 */
import type { Attribute, AttributeType } from '../domain/index.js';
import type { ServerSchema } from '../adapters.js';

export interface SourceInput {
  id: string;
  name: string;
  /** 该源 ds 级编译 schema；null = 该源还没编译过（或空壳） */
  schema: ServerSchema | null;
}

export interface ConceptLanding {
  sourceId: string;
  entityName: string;
  /** 源 schema 再生后这条对齐已失效——失效落点不画线、不标概念 */
  stale: boolean;
}

export interface ConceptInput {
  name: string;
  landings: ConceptLanding[];
}

export interface MultiSourceEntity {
  /** `${sourceId}:${实体名}` —— 命名空间化，不同源同名不并 */
  id: string;
  name: string;
  displayName?: string;
  attributes: Attribute[];
  /** 所属概念名（去重；来自未失效落点） */
  concepts: string[];
  sourceId: string;
  sourceName: string;
  /** 在 sources 里的序号——调色板按下标取色 */
  sourceIndex: number;
}

export interface MultiSourceRelation {
  id: string;
  name: string;
  sourceEntityId: string;
  targetEntityId: string;
  /** 关系属于哪个源（颜色/过滤按它算） */
  sourceId: string;
}

export interface ConceptLink {
  id: string;
  concept: string;
  sourceEntityId: string;
  targetEntityId: string;
}

export interface MultiSourceSource {
  id: string;
  name: string;
  entityCount: number;
  hasSchema: boolean;
}

export interface MultiSourceModel {
  sources: MultiSourceSource[];
  entities: MultiSourceEntity[];
  relations: MultiSourceRelation[];
  /** 同一概念的**跨源**落点两两配对 */
  conceptLinks: ConceptLink[];
  /** 有任何跨源概念落点的节点 id（「只看跨源」与面板计数用） */
  crossSourceEntityIds: string[];
}

/** 节点 id 的命名空间规则——只有这一处定义，别处不许自己拼。 */
export const entityNodeId = (sourceId: string, entityName: string): string => `${sourceId}:${entityName}`;

const ATTRIBUTE_TYPES: readonly string[] = ['string', 'text', 'number', 'integer', 'boolean', 'date', 'datetime', 'enum'];

function toAttribute(nodeId: string, raw: {
  name: string;
  type?: string;
  required?: boolean;
  is_primary_key?: boolean;
  enum_values?: string[];
}): Attribute {
  const attribute: Attribute = {
    id: `${nodeId}.${raw.name}`,
    name: raw.name,
    type: (raw.type && ATTRIBUTE_TYPES.includes(raw.type) ? raw.type : 'string') as AttributeType,
    required: raw.required === true,
    primaryKey: raw.is_primary_key === true,
  };
  if (raw.enum_values?.length) attribute.enumValues = [...raw.enum_values];
  return attribute;
}

export function buildMultiSourceModel(
  sources: readonly SourceInput[],
  concepts: readonly ConceptInput[],
): MultiSourceModel {
  // ① 概念先落位：节点 id → 概念名集合（失效落点直接跳过）
  const conceptByNode = new Map<string, string[]>();
  for (const concept of concepts) {
    for (const landing of concept.landings) {
      if (landing.stale) continue;
      const nodeId = entityNodeId(landing.sourceId, landing.entityName);
      const list = conceptByNode.get(nodeId) ?? [];
      if (!list.includes(concept.name)) list.push(concept.name);
      conceptByNode.set(nodeId, list);
    }
  }

  // ② 各源实体/关系展开 + 命名空间化
  const entities: MultiSourceEntity[] = [];
  const relations: MultiSourceRelation[] = [];
  const nodeIds = new Set<string>();

  sources.forEach((source, sourceIndex) => {
    const rawEntities = source.schema?.entity_types ?? [];
    const localNames = new Set(rawEntities.map((item) => item.name));
    for (const raw of rawEntities) {
      const id = entityNodeId(source.id, raw.name);
      nodeIds.add(id);
      entities.push({
        id,
        name: raw.name,
        ...(raw.display_name ? { displayName: raw.display_name } : {}),
        attributes: (raw.attributes ?? []).map((attribute) => toAttribute(id, attribute)),
        concepts: conceptByNode.get(id) ?? [],
        sourceId: source.id,
        sourceName: source.name,
        sourceIndex,
      });
    }
    for (const raw of source.schema?.relation_types ?? []) {
      // 端点必须落在本源的实体集合里——服务端理论上保证，防一手：
      // 悬空端点喂给 React Flow 只会得到一张画不出的边 + 控制台噪音
      if (!localNames.has(raw.source) || !localNames.has(raw.target)) continue;
      relations.push({
        id: `${source.id}:${raw.name}`,
        name: raw.name,
        sourceEntityId: entityNodeId(source.id, raw.source),
        targetEntityId: entityNodeId(source.id, raw.target),
        sourceId: source.id,
      });
    }
  });

  // ③ 跨源概念连线：只连**不同源**的落点两两配对（同源两条落点不画线——
  //    线要表达的是「跨源」；它们照样各自带概念、各自高亮）。
  //    落点目标实体不存在的（stale 之外的脏数据）也丢掉。
  const conceptLinks: ConceptLink[] = [];
  const linkedNodeIds = new Set<string>();
  for (const concept of concepts) {
    const landings = concept.landings
      .filter((landing) => !landing.stale)
      .map((landing) => ({ ...landing, nodeId: entityNodeId(landing.sourceId, landing.entityName) }))
      .filter((landing) => nodeIds.has(landing.nodeId));
    for (let i = 0; i < landings.length; i += 1) {
      for (let j = i + 1; j < landings.length; j += 1) {
        if (landings[i].sourceId === landings[j].sourceId) continue;
        conceptLinks.push({
          id: `x:${concept.name}:${landings[i].nodeId}:${landings[j].nodeId}`,
          concept: concept.name,
          sourceEntityId: landings[i].nodeId,
          targetEntityId: landings[j].nodeId,
        });
        linkedNodeIds.add(landings[i].nodeId);
        linkedNodeIds.add(landings[j].nodeId);
      }
    }
  }

  return {
    sources: sources.map((source) => {
      const rawEntities = source.schema?.entity_types ?? [];
      return {
        id: source.id,
        name: source.name,
        entityCount: rawEntities.length,
        hasSchema: rawEntities.length > 0 || (source.schema?.relation_types?.length ?? 0) > 0,
      };
    }),
    entities,
    relations,
    conceptLinks,
    crossSourceEntityIds: [...linkedNodeIds],
  };
}
