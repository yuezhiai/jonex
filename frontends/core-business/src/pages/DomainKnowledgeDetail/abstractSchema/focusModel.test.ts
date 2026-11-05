import { describe, expect, it } from 'vitest';

import { buildNeighborGroups } from './focusModel.js';

describe('buildNeighborGroups', () => {
  it('按经由关系名分组；同一邻居出现在多条路径的组里', () => {
    const groups = buildNeighborGroups([
      { dataSourceId: 'ds_a', entityName: 'Application', sourceName: '招聘库',
        via: [{ seedDataSourceId: 'ds_a', seedEntityName: 'Job', relationName: 'REF_APP' }] },
      { dataSourceId: 'ds_b', entityName: 'Jobs', sourceName: '订单库',
        via: [{ seedDataSourceId: 'ds_a', seedEntityName: 'Job', relationName: 'REF_APP' },
              { seedDataSourceId: 'ds_b', seedEntityName: 'Orders', relationName: 'REF_X' }] },
    ]);
    expect(groups.map((g) => g.relationName)).toEqual(['REF_APP', 'REF_X']);
    expect(groups[0].items.map((i) => i.entityName)).toEqual(['Application', 'Jobs']);
    expect(groups[1].items.map((i) => i.entityName)).toEqual(['Jobs']);
  });

  it('同一邻居经两枚种子同名关系拉入时，组内只出现一次', () => {
    const groups = buildNeighborGroups([
      { dataSourceId: 'ds_a', entityName: 'Job', sourceName: '招聘库',
        via: [{ seedDataSourceId: 'ds_a', seedEntityName: 'Application', relationName: 'REF_APP' },
              { seedDataSourceId: 'ds_a', seedEntityName: 'Company', relationName: 'REF_APP' }] },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].items.map((i) => i.entityName)).toEqual(['Job']);
    expect(groups[0].items).toHaveLength(1);
  });

  it('空输入给空数组', () => {
    expect(buildNeighborGroups([])).toEqual([]);
  });
});
