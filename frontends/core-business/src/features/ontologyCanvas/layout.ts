/**
 * 自动布局。
 *
 * 本移植**不持久化节点坐标**——Jonex 的 schema 里没有存画布位置的地方，
 * 也刻意不为此加字段。所以每次加载文档都按关系图重算一遍。
 *
 * ## 为什么不用 dagre
 *
 * 一开始用的是 dagre（nexonto 的做法，`rankdir: 'LR'`）。实测在 assets_r2 上
 * **不可用**：这个库的关系是**星型**的——绝大多数表都指向 `t_asset` /
 * `t_asset_instance` 这几个中心表，于是它们**都没有入边**，被 dagre 全部排进第 0 层。
 * 横向布局下同一层是纵向堆叠的，结果坐标范围是 **x: 2250 × y: 13874** ——
 * 一条细长条，滚十屏还在同一层。
 *
 * dagre 的价值在于把**有向层级**画清楚；当图本身没有层级（星型/扇入型）时，
 * 它给不出有意义的排序，只剩下「同层堆叠」这个副作用。
 *
 * ## 网格 + 连接度排序
 *
 * 换成一个可预期的网格：按连接度降序排（hub 表在第一行，一眼看到这是张星型图），
 * 然后按列数切开。没有算法调参、结果稳定可复现，尺寸也可以直接算出来。
 * 代价是连线会跨行绕——对这个用途（找表、改别名、连关系）可以接受。
 *
 * ## 多源视图的区域布局（layoutRegions）
 *
 * 同一个「网格 + 连接度排序」按源分组复用：每个源一块网格，从左到右依次排开，
 * 供只读多源画布按源分区渲染（区域框由视图层画，这里只算坐标与框）。
 */

import type { DomainDocument, EntityType } from './domain/index.js';

/** 节点尺寸。与 `.entity-node` 的 CSS 宽度保持一致（220px）。 */
export const NODE_WIDTH = 220;
/** 节点高度的**基础预算**（6 条属性预览 + 两行来源徽章）。超出的徽章行按行加高，见 `estimateNodeHeight`。 */
export const NODE_HEIGHT = 250;

/** 徽章行高：`.entity-node-source` 的 line-height 16px + `.entity-node-sources` 的 gap 3px。 */
export const BADGE_ROW_HEIGHT = 19;
/** 220px 卡片一行大致放得下的徽章数（徽章 max-width 100px）——保守取 2。 */
const BADGES_PER_ROW = 2;
/** `NODE_HEIGHT` 预算已覆盖的徽章行数；超出部分逐行加高。 */
const BADGE_ROWS_IN_BUDGET = 2;

const GAP_X = 40;
const GAP_Y = 40;
const ORIGIN_X = 40;
const ORIGIN_Y = 40;

/** 每行放几个。86 个实体按 8 列约排 11 行，宽 2040 × 高 3190——缩到 0.4 能看全貌。 */
const COLUMNS = 8;

/** 区域外扩的留白（源名标签要放在框里） */
export const REGION_PAD = 16;
/** 区域标题占位高度：首行节点从这里之下开始，避免压住数据源名。 */
export const REGION_HEADER = 28;
/** 相邻区域之间的横向间距 */
const REGION_GAP = 96;

/**
 * 节点高度的保守预算：徽章（**included** 落点、按源去重）超过 `NODE_HEIGHT` 已覆盖的两行时，
 * 每多一行加 `BADGE_ROW_HEIGHT`。
 *
 * 为什么要这一步：徽章是可换行的，≥3 行时节点实际高度会超过 250——按固定网格排下一行
 * 就会被节点底边压住（真机触发条件见 `.superpowers/sdd/abstract-v2-phase2-progress.md`
 * 的全波终审 Minor ①）。布局按预算高度排，压叠从根上消失。
 */
export function estimateNodeHeight(entity: Pick<EntityType, 'landings'>): number {
  const sources = new Set<string>();
  for (const landing of entity.landings ?? []) {
    if (landing.included === false) continue;
    sources.add(landing.dataSourceId);
  }
  const badgeRows = Math.ceil(sources.size / BADGES_PER_ROW);
  return NODE_HEIGHT + Math.max(0, badgeRows - BADGE_ROWS_IN_BUDGET) * BADGE_ROW_HEIGHT;
}

/**
 * 按列把一组 id 摆成网格；**行高取该行节点的最高预算**，下一行从这里继续。
 * 返回坐标与整块网格的高度（不含最后一行的 `GAP_Y`）。
 */
function layoutGrid(
  ids: readonly string[],
  columns: number,
  originX: number,
  originY: number,
  heightOf: (id: string) => number,
): { positions: Record<string, { x: number; y: number }>; height: number } {
  const positions: Record<string, { x: number; y: number }> = {};
  const cols = Math.max(1, columns);
  let y = originY;
  for (let start = 0; start < ids.length; start += cols) {
    const rowIds = ids.slice(start, start + cols);
    let rowHeight = 0;
    rowIds.forEach((id, col) => {
      positions[id] = { x: originX + col * (NODE_WIDTH + GAP_X), y };
      rowHeight = Math.max(rowHeight, heightOf(id));
    });
    y += rowHeight + GAP_Y;
  }
  return { positions, height: ids.length ? y - originY - GAP_Y : 0 };
}

/**
 * 连接度降序（出边+入边）给实体排序；同度按名字——排序键必须全能定序，
 * 否则布局会在两次加载间跳动。
 */
export function orderByDegree<T extends { id: string; name: string }>(
  entities: readonly T[],
  relations: ReadonlyArray<{ sourceEntityId: string; targetEntityId: string }>,
): T[] {
  const degree = new Map<string, number>(entities.map((entity) => [entity.id, 0]));
  for (const relation of relations) {
    if (degree.has(relation.sourceEntityId)) degree.set(relation.sourceEntityId, (degree.get(relation.sourceEntityId) ?? 0) + 1);
    if (degree.has(relation.targetEntityId)) degree.set(relation.targetEntityId, (degree.get(relation.targetEntityId) ?? 0) + 1);
  }
  return [...entities].sort((left, right) => {
    const byDegree = (degree.get(right.id) ?? 0) - (degree.get(left.id) ?? 0);
    return byDegree !== 0 ? byDegree : left.name.localeCompare(right.name);
  });
}

/**
 * 按连接度降序给全部实体算坐标（编辑画布用）。
 *
 * 连接度 = 出边 + 入边。中心表（被几十张表引用）排最前，符合「先看主干」的阅读顺序。
 * 同度数的按名字排——保证同一份 schema 每次加载得到完全相同的布局。
 */
export function layoutAll(
  document: DomainDocument,
  columns = COLUMNS,
): Record<string, { x: number; y: number }> {
  const ordered = orderByDegree(document.model.entities, document.model.relations);
  const byId = new Map(ordered.map((entity) => [entity.id, entity]));
  return layoutGrid(
    ordered.map((entity) => entity.id), columns, ORIGIN_X, ORIGIN_Y,
    (id) => estimateNodeHeight(byId.get(id)!),
  ).positions;
}

export interface SourceRegion {
  sourceId: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 多源视图的区域布局：每个源一块网格（组内顺序由**调用方**先按需排好——
 * 这里是纯摆放，不重排），从左到右依次排开；返回坐标与区域框。
 * 空组也给一个占位框（前端要在框里写「本源还没有编译 schema」）。
 */
export function layoutRegions(
  groups: ReadonlyArray<{ sourceId: string; entityIds: readonly string[] }>,
  columns = COLUMNS,
  heightOf: (id: string) => number = () => NODE_HEIGHT,
): { positions: Record<string, { x: number; y: number }>; regions: SourceRegion[] } {
  const positions: Record<string, { x: number; y: number }> = {};
  const regions: SourceRegion[] = [];
  let cursorX = ORIGIN_X;

  for (const group of groups) {
    const count = group.entityIds.length;
    const cols = Math.max(1, Math.min(columns, count));
    const gridWidth = cols * NODE_WIDTH + (cols - 1) * GAP_X;
    const { positions: groupPositions, height: gridHeight } = layoutGrid(
      group.entityIds, cols, cursorX, ORIGIN_Y + REGION_HEADER, heightOf,
    );
    Object.assign(positions, groupPositions);

    regions.push({
      sourceId: group.sourceId,
      x: cursorX - REGION_PAD,
      y: ORIGIN_Y - REGION_PAD,
      width: gridWidth + REGION_PAD * 2,
      // 空组也要给占位框（前端要画「本源还没有编译 schema」）——沿用 NODE_HEIGHT 的高度。
      height: REGION_HEADER + (count ? gridHeight : NODE_HEIGHT) + REGION_PAD * 2,
    });
    cursorX += gridWidth + REGION_PAD * 2 + REGION_GAP;
  }

  return { positions, regions };
}
