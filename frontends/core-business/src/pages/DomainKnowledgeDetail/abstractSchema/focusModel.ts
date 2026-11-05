/**
 * 聚焦生成弹窗的纯逻辑（node 环境可测）：邻居预览按「经由关系名」分组。
 *
 * 同一邻居可能被多枚种子/多条关系拉进来 —— 它在每个关系组里**至多**出现
 * 一次（组内按源 + 实体去重，同一邻居经两枚种子走同名关系不重复），
 * 组间按关系名字典序、组内按源 + 实体排序，保证渲染稳定。
 */
import type { FocusNeighbor } from '@/api/abstractSchema';

export interface NeighborItem {
  dataSourceId: string;
  entityName: string;
  sourceName: string;
}

export interface NeighborGroup {
  relationName: string;
  items: NeighborItem[];
}

export function buildNeighborGroups(neighbors: FocusNeighbor[]): NeighborGroup[] {
  const byRel = new Map<string, NeighborGroup>();
  const seenByRel = new Map<string, Set<string>>();
  for (const n of neighbors) {
    const entityKey = `${n.dataSourceId}/${n.entityName}`;
    for (const v of n.via) {
      let g = byRel.get(v.relationName);
      if (!g) {
        g = { relationName: v.relationName, items: [] };
        byRel.set(v.relationName, g);
        seenByRel.set(v.relationName, new Set());
      }
      const seen = seenByRel.get(v.relationName) as Set<string>;
      if (seen.has(entityKey)) continue;
      seen.add(entityKey);
      g.items.push({
        dataSourceId: n.dataSourceId,
        entityName: n.entityName,
        sourceName: n.sourceName ?? '',
      });
    }
  }
  const groups = [...byRel.values()];
  for (const g of groups) {
    g.items.sort((a, b) =>
      `${a.dataSourceId}/${a.entityName}`.localeCompare(`${b.dataSourceId}/${b.entityName}`),
    );
  }
  groups.sort((a, b) => a.relationName.localeCompare(b.relationName));
  return groups;
}
