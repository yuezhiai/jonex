/**
 * 节点的来源徽章（抽象态）：每个 **included** 落点一枚，按 dataSourceId 去重。
 *
 * 颜色映射是**文档级**的：同一份文档里同一个源必须同色（逐节点各算各的次序会漂移）。
 * 索引按 dataSourceId 在文档中的**首次出现顺序**分配；调色板与多源视图同源，
 * 但顺序口径不同（多源视图按数据源清单排）—— 两边颜色不保证逐一相同，视觉语言一致即可。
 */
import type { EntityType } from '../domain/index.js';
import { sourceColor } from '../multiSource/palette.js';

export interface SourceBadge {
  dataSourceId: string;
  label: string;
  color: string;
}

/** 文档级颜色映射：dataSourceId → 色值（按首次出现顺序）。 */
export function buildSourceColorMap(entities: readonly EntityType[]): Map<string, string> {
  const map = new Map<string, string>();
  for (const entity of entities) {
    for (const landing of entity.landings ?? []) {
      if (!map.has(landing.dataSourceId)) map.set(landing.dataSourceId, sourceColor(map.size));
    }
  }
  return map;
}

/** 该实体的徽章：只取 included 落点（不纳入的不上节点——面板里看得到），按 dataSourceId 去重。 */
export function entitySourceBadges(entity: EntityType, colorMap: ReadonlyMap<string, string>): SourceBadge[] {
  const seen = new Set<string>();
  const badges: SourceBadge[] = [];
  for (const landing of entity.landings ?? []) {
    if (landing.included === false || seen.has(landing.dataSourceId)) continue;
    seen.add(landing.dataSourceId);
    badges.push({
      dataSourceId: landing.dataSourceId,
      // sourceName 缺失（老数据/源已删）回落 id：至少可拿去对账（与 ConceptCard 同口径）
      label: landing.sourceName || landing.dataSourceId,
      color: colorMap.get(landing.dataSourceId) ?? sourceColor(0),
    });
  }
  return badges;
}
