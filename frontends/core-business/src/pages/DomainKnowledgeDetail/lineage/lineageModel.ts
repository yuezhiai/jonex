/**
 * 血缘下钻的纯逻辑（node 环境可测，不依赖 React / antd）。
 *
 * 抽成独立模块是为了给「上游/下游」方向上锁：方向写反是本页最难肉眼发现的错，
 * 且对调后 typecheck 与既有测试都不会响 —— 见 `lineageModel.test.ts` 的反向断言。
 *
 * 比较一律用 `表.列` 全串 —— **同一张表的不同列是不同节点**，不能只看表名。
 */
import type { LineageEdge } from '@/api/lineage';

export type DrillDir = 'up' | 'down';

/** 端点键：`表.列` 全串。**列级**比较——同一张表的不同列是不同节点。 */
export const nodeKey = (table: string, column: string) => `${table}.${column}`;

/**
 * 上游 = `to` 端命中该节点的边（谁流向它）；下游 = `from` 端命中（它流向谁）。
 *
 * 纯前端筛选：列表按源整取（一个源的血缘边通常在几十条量级），
 * 没有分页压力，也就不为下钻单开接口。
 */
export function filterByNode(edges: LineageEdge[], node: string, dir: DrillDir): LineageEdge[] {
  return dir === 'up'
    ? edges.filter((e) => nodeKey(e.to_table, e.to_column) === node)
    : edges.filter((e) => nodeKey(e.from_table, e.from_column) === node);
}
