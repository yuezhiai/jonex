import { describe, expect, it } from 'vitest';

import {
  BADGE_ROW_HEIGHT, estimateNodeHeight, layoutAll, layoutRegions, NODE_HEIGHT, NODE_WIDTH,
  orderByDegree, REGION_HEADER, REGION_PAD,
} from './layout.js';
import type { DomainDocument, EntityLanding, EntityType } from './domain/index.js';

const landing = (dataSourceId: string, included = true): EntityLanding => ({
  dataSourceId, entityName: 'e', included,
});
const entity = (id: string, landings: EntityLanding[] = []): EntityType => ({
  id, name: id, aliases: [], attributes: [], landings,
});
const doc = (entities: EntityType[]): DomainDocument => ({
  storageVersion: 1,
  schemaVersion: 1,
  revision: 1,
  domain: { id: 'd', displayName: 'd' },
  model: {
    entities, relations: [], constraints: [],
    disambiguation: { caseInsensitive: true, aliasMerge: true },
  },
  editor: { nodePositions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
  savedAt: '2026-10-23T00:00:00Z',
});

describe('layoutRegions', () => {
  it('每个源的节点落在自己的区域框内，且相邻区域不重叠', () => {
    const idsA = Array.from({ length: 10 }, (_, index) => `a:${index}`);
    const idsB = Array.from({ length: 3 }, (_, index) => `b:${index}`);

    const { positions, regions } = layoutRegions([
      { sourceId: 'a', entityIds: idsA },
      { sourceId: 'b', entityIds: idsB },
    ]);

    expect(regions).toHaveLength(2);
    for (const id of idsA) {
      expect(positions[id].x).toBeGreaterThanOrEqual(regions[0].x);
      expect(positions[id].x + NODE_WIDTH).toBeLessThanOrEqual(regions[0].x + regions[0].width);
    }
    for (const id of idsB) {
      expect(positions[id].x).toBeGreaterThanOrEqual(regions[1].x);
      expect(positions[id].x + NODE_WIDTH).toBeLessThanOrEqual(regions[1].x + regions[1].width);
    }
    expect(regions[1].x).toBeGreaterThanOrEqual(regions[0].x + regions[0].width);
  });

  it('首行节点给区域标题留出空间（不压住数据源名）', () => {
    const { positions, regions } = layoutRegions([{ sourceId: 'a', entityIds: ['a:0'] }]);

    // 区域标题画在框内顶部（约 24px 高）；首行节点必须落在标题下方
    expect(positions['a:0'].y - regions[0].y).toBeGreaterThanOrEqual(REGION_PAD + REGION_HEADER);
  });

  it('空区域也给占位框（前端要画「本源还没有编译 schema」）', () => {
    const { regions } = layoutRegions([{ sourceId: 'empty', entityIds: [] }]);

    expect(regions).toHaveLength(1);
    expect(regions[0].width).toBeGreaterThan(0);
    expect(regions[0].height).toBeGreaterThan(0);
  });

  it('确定：同一输入两次得到同一布局', () => {
    const groups = [{ sourceId: 'a', entityIds: ['x', 'y', 'z'] }];

    expect(layoutRegions(groups)).toEqual(layoutRegions(groups));
  });
});

describe('orderByDegree', () => {
  it('连接度降序、同度按名字——hub 在前且定序全能', () => {
    const entities = [{ id: 'e:B', name: 'B' }, { id: 'e:A', name: 'A' }, { id: 'e:C', name: 'C' }];
    const relations = [
      { sourceEntityId: 'e:A', targetEntityId: 'e:B' },
      { sourceEntityId: 'e:C', targetEntityId: 'e:B' },
    ];

    expect(orderByDegree(entities, relations).map((entity) => entity.id)).toEqual(['e:B', 'e:A', 'e:C']);
  });
});

describe('estimateNodeHeight（徽章高度预算）', () => {
  it('≤2 行徽章（≤4 个 source）仍在 NODE_HEIGHT 预算内', () => {
    expect(estimateNodeHeight(entity('e', []))).toBe(NODE_HEIGHT);
    expect(estimateNodeHeight(entity('e', [1, 2, 3, 4].map((n) => landing(`s${n}`)))))
      .toBe(NODE_HEIGHT);
  });

  it('第 3 行徽章起逐行加高（≥3 行是台账里的触发条件）', () => {
    expect(estimateNodeHeight(entity('e', [1, 2, 3, 4, 5].map((n) => landing(`s${n}`)))))
      .toBe(NODE_HEIGHT + BADGE_ROW_HEIGHT);
  });

  it('只数 included 且按源去重', () => {
    const landings = [
      landing('s1'), landing('s1'), landing('s2'), landing('s3'), landing('s4'),
      landing('s9', false),
    ];
    expect(estimateNodeHeight(entity('e', landings))).toBe(NODE_HEIGHT);
  });
});

describe('layoutAll（按真实高度排下一行）', () => {
  it('某行有高节点时，下一行整体下移相应高度', () => {
    const plain = Array.from({ length: 9 }, (_, index) => entity(`e:${index}`));
    const tall = plain.map((item) => entity(item.id));
    tall[0] = entity('e:0', [1, 2, 3, 4, 5].map((n) => landing(`s${n}`)));

    const base = layoutAll(doc(plain));
    const shifted = layoutAll(doc(tall));

    // 第 0 行第 0 个节点多出 1 行徽章 → 第 1 行整体下移 BADGE_ROW_HEIGHT
    expect(shifted['e:8'].y - shifted['e:0'].y)
      .toBe(base['e:8'].y - base['e:0'].y + BADGE_ROW_HEIGHT);
    // 同一行内的节点仍然同高
    expect(shifted['e:1'].y).toBe(shifted['e:0'].y);
  });
});

describe('layoutRegions（可选的逐节点高度）', () => {
  it('首行有高节点时区域高度与下一行一起增长', () => {
    const entityIds = Array.from({ length: 10 }, (_, index) => `a:${index}`);
    const base = layoutRegions([{ sourceId: 'a', entityIds }]);
    const tall = layoutRegions(
      [{ sourceId: 'a', entityIds }],
      8,
      (id) => (id === 'a:0' ? NODE_HEIGHT + BADGE_ROW_HEIGHT : NODE_HEIGHT),
    );

    expect(tall.positions['a:8'].y - base.positions['a:8'].y).toBe(BADGE_ROW_HEIGHT);
    expect(tall.regions[0].height).toBe(base.regions[0].height + BADGE_ROW_HEIGHT);
  });
});
