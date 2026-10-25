import { describe, expect, it } from 'vitest';

import { buildSourceColorMap, entitySourceBadges } from './entitySources.js';
import type { EntityLanding, EntityType } from '../domain/index.js';

const landing = (dataSourceId: string, entityName: string, included = true, sourceName?: string): EntityLanding => ({
  dataSourceId, entityName, included, sourceName,
});
const entity = (id: string, landings: EntityLanding[]): EntityType => ({
  id, name: id, aliases: [], attributes: [], landings,
});

describe('buildSourceColorMap', () => {
  it('按 dataSourceId 首次出现顺序取色；同源跨实体同色', () => {
    const map = buildSourceColorMap([
      entity('e:A', [landing('s1', 'a'), landing('s2', 'b')]),
      entity('e:B', [landing('s2', 'b2'), landing('s3', 'c')]),
    ]);
    expect([...map.keys()]).toEqual(['s1', 's2', 's3']);
    expect(new Set(map.values()).size).toBe(3);
  });
});

describe('entitySourceBadges', () => {
  it('只取 included；同源去重；sourceName 缺失回落 dataSourceId', () => {
    const badges = entitySourceBadges(
      entity('e:A', [
        landing('s1', 'a', true, 'jinpin'),
        landing('s1', 'a2', true, 'jinpin'),
        landing('s2', 'b', false, 'assets'),
        landing('s3', 'c', true),
      ]),
      new Map([['s1', '#111'], ['s3', '#333']]),
    );
    expect(badges).toEqual([
      { dataSourceId: 's1', label: 'jinpin', color: '#111' },
      { dataSourceId: 's3', label: 's3', color: '#333' },
    ]);
  });

  it('没有落点 → 空数组（编译画布节点无徽章）', () => {
    expect(entitySourceBadges(entity('e:Plain', []), new Map())).toEqual([]);
  });
});
