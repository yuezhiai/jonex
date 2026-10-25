import { describe, expect, it } from 'vitest';

import { buildMultiSourceModel, entityNodeId, type ConceptInput, type SourceInput } from './model.js';

const srcA: SourceInput = {
  id: 'ds-a',
  name: 'jinpin',
  schema: {
    entity_types: [
      { name: 'User', attributes: [{ name: 'id', type: 'number', is_primary_key: true }] },
      { name: 'Order', attributes: [] },
    ],
    relation_types: [{ name: 'USER_ORDER', source: 'User', target: 'Order' }],
  },
};

const srcB: SourceInput = {
  id: 'ds-b',
  name: 'assets_r2',
  schema: {
    entity_types: [
      { name: 'User', attributes: [{ name: 'uid', type: 'string' }] },
      { name: 'Asset', attributes: [] },
    ],
    relation_types: [{ name: 'USER_ASSET', source: 'User', target: 'Asset' }],
  },
};

describe('buildMultiSourceModel', () => {
  it('不同源同名实体不并——各自命名空间出节点', () => {
    const model = buildMultiSourceModel([srcA, srcB], []);
    const users = model.entities.filter((entity) => entity.name === 'User');

    expect(users).toHaveLength(2);
    expect(new Set(users.map((entity) => entity.id)).size).toBe(2);
    expect(users.map((entity) => entity.sourceName).sort()).toEqual(['assets_r2', 'jinpin']);
  });

  it('同源关系重映射到命名空间 id；端点不在本源实体集合里的关系丢弃', () => {
    const withDangling: SourceInput = {
      ...srcA,
      schema: {
        ...srcA.schema,
        relation_types: [
          ...(srcA.schema!.relation_types ?? []),
          { name: 'GHOST', source: 'User', target: 'NotThere' },
        ],
      },
    };

    const model = buildMultiSourceModel([withDangling, srcB], []);
    const relation = model.relations.find((item) => item.name === 'USER_ORDER');

    expect(relation?.sourceEntityId).toBe(entityNodeId('ds-a', 'User'));
    expect(relation?.targetEntityId).toBe(entityNodeId('ds-a', 'Order'));
    expect(model.relations.some((item) => item.name === 'GHOST')).toBe(false);
  });

  it('跨源概念连线：只连不同源落点；同源落点不画线但都算跨源节点、都带概念', () => {
    const concepts: ConceptInput[] = [{
      name: 'User',
      landings: [
        { sourceId: 'ds-a', entityName: 'User', stale: false },
        { sourceId: 'ds-b', entityName: 'User', stale: false },
        { sourceId: 'ds-a', entityName: 'Order', stale: false },
      ],
    }];

    const model = buildMultiSourceModel([srcA, srcB], concepts);

    // 三条落点两两配对 3 对，其中同源的 (ds-a:User, ds-a:Order) 不画 → 剩 2 条
    expect(model.conceptLinks).toHaveLength(2);
    expect(model.conceptLinks.every((link) => link.concept === 'User')).toBe(true);
    expect([...model.crossSourceEntityIds].sort()).toEqual(
      [entityNodeId('ds-a', 'User'), entityNodeId('ds-b', 'User'), entityNodeId('ds-a', 'Order')].sort(),
    );
    expect(model.entities.find((entity) => entity.id === entityNodeId('ds-a', 'Order'))?.concepts).toEqual(['User']);
  });

  it('失效落点不连线、不标概念', () => {
    const concepts: ConceptInput[] = [{
      name: 'User',
      landings: [
        { sourceId: 'ds-a', entityName: 'User', stale: false },
        { sourceId: 'ds-b', entityName: 'User', stale: true },
      ],
    }];

    const model = buildMultiSourceModel([srcA, srcB], concepts);

    expect(model.conceptLinks).toHaveLength(0);
    expect(model.crossSourceEntityIds).toHaveLength(0);
    expect(model.entities.find((entity) => entity.id === entityNodeId('ds-b', 'User'))?.concepts).toEqual([]);
  });

  it('落点实体不存在时跳过；schema 为 null 的源计数为 0 且 hasSchema=false', () => {
    const concepts: ConceptInput[] = [{
      name: 'User',
      landings: [
        { sourceId: 'ds-a', entityName: 'User', stale: false },
        { sourceId: 'ds-b', entityName: 'Gone', stale: false },
      ],
    }];

    const model = buildMultiSourceModel([srcA, { id: 'ds-b', name: 'assets_r2', schema: null }], concepts);

    expect(model.conceptLinks).toHaveLength(0);
    expect(model.sources.find((source) => source.id === 'ds-b')).toMatchObject({ entityCount: 0, hasSchema: false });
  });
});
