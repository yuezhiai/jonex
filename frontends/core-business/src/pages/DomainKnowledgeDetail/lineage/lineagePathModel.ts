/**
 * 血缘画布的路径高亮模型（纯函数，node 环境可测，不依赖 React / DOM）。
 *
 * 输入是 `buildLineageGraph` 产出的画布边 + `GET /ontology/lineage-paths` 的评分路径，
 * 输出是「打了高亮标记的边」与「路径清单」。组件只负责渲染，不做第二套映射。
 *
 * 映射口径：
 * - 画布边 id 是 `from->to`（**数据流方向**，与 `lineageGraphModel` 同一 helper
 *   `lineageEdgeId`）；每个 step 按 `step.from_table->step.to_table` 找边。后端 step
 *   端点已经是数据流端点（`reversed` 只标遍历方向），不能拿遍历方向去反推边 id。
 * - 一条路径一个固定色（`PATH_HIGHLIGHT_PALETTE`），路径 i（0-based）用
 *   `palette[i % len]`；**重叠边取最先出现的那条路径的颜色**，并把所有命中它的
 *   路径序号（1-based）累积进 `data.pathIndexes`。
 * - 端点不在画布边里的 step 直接忽略（路径可能经过画布未展开的边 —— 只高亮
 *   画布上画得出来的部分，不报错）。
 * - 非高亮边原样返回（同一对象引用）；高亮边是浅拷贝，不改输入。
 * - 空路径 → 边不变 + 空清单。
 */
import type { Edge } from '@xyflow/react';

import type { LineagePath, LineagePathStepType } from '@/api/ontologyLineagePaths';
import { lineageEdgeId } from './lineageGraphModel';

/** 路径高亮的固定色板（按路径顺序循环使用）。 */
export const PATH_HIGHLIGHT_PALETTE = [
  '#f5222d',
  '#fa8c16',
  '#13c2c2',
  '#722ed1',
  '#eb2f96',
] as const;

/** 高亮边线宽（压过置信度编码的 1..3 上限，确保被高亮的边一眼可辨）。 */
export const PATH_HIGHLIGHT_STROKE_WIDTH = 4;

/** 高亮往边 `data` 上追加的字段。 */
export interface LineagePathHighlightData extends Record<string, unknown> {
  /** 命中该边的路径序号（1-based，按输入 `paths` 顺序，去重）。 */
  pathIndexes?: number[];
}

/** 打了高亮标记的画布边；未高亮的边只是类型上多挂了一个可选字段。 */
export type PathHighlightedEdge<D extends Record<string, unknown>> = Edge<
  D & LineagePathHighlightData
>;

/** 路径清单里的一跳（camelCase，与画布展示口径一致）。 */
export interface PathSummaryStep {
  fromTable: string;
  fromColumn: string;
  toTable: string;
  toColumn: string;
  type: LineagePathStepType;
  origin: string;
  reversed: boolean;
}

/** 路径清单项：`index` 为 1-based 路径序号。 */
export interface PathSummary {
  index: number;
  score: number;
  transfers: number;
  hitsTarget: boolean;
  steps: PathSummaryStep[];
}

export interface PathHighlightModel<D extends Record<string, unknown>> {
  edges: Array<PathHighlightedEdge<D>>;
  pathSummaries: PathSummary[];
}

/** 内部：边 id → 高亮信息（路径遍历时累积）。 */
interface HighlightEntry {
  color: string;
  pathIndexes: number[];
}

/**
 * 把路径落成画布高亮。
 *
 * `edges` 只读消费：非高亮边原样返回（同一引用），高亮边返回浅拷贝
 * （`style` 与 `data` 也是新对象），不修改入参。
 */
export function applyPathHighlights<D extends Record<string, unknown>>(
  edges: ReadonlyArray<Edge<D>>,
  paths: ReadonlyArray<LineagePath>,
): PathHighlightModel<D> {
  const pathSummaries: PathSummary[] = paths.map((path, i) => ({
    index: i + 1,
    score: path.score,
    transfers: path.transfers,
    hitsTarget: path.hits_target,
    steps: path.steps.map((step) => ({
      fromTable: step.from_table,
      fromColumn: step.from_column,
      toTable: step.to_table,
      toColumn: step.to_column,
      type: step.type,
      origin: step.origin,
      reversed: step.reversed,
    })),
  }));

  if (paths.length === 0) {
    // 空路径：边原样（同一对象引用）、清单为空。
    return {
      edges: edges.slice() as PathHighlightedEdge<D>[],
      pathSummaries,
    };
  }

  const edgeIds = new Set(edges.map((edge) => edge.id));
  const highlightById = new Map<string, HighlightEntry>();

  paths.forEach((path, i) => {
    const color = PATH_HIGHLIGHT_PALETTE[i % PATH_HIGHLIGHT_PALETTE.length];
    const pathIndex = i + 1;
    // 同一条路径里若重复经过同一条边（环/重复 step），路径序号只记一次。
    const seenInPath = new Set<string>();
    for (const step of path.steps) {
      // 画布边全部来自 `DataSourceEntity` 的 `DERIVED_FROM` 血缘投影（join 边
      // 只在编译 schema 里、不在画布上）。只按表对匹配会让同一表对上的 join
      // step 去高亮一条其实属于 lineage 的边 —— 类型对不上，直接跳过。
      if (step.type !== 'lineage') continue;
      const id = lineageEdgeId(step.from_table, step.to_table);
      if (!edgeIds.has(id) || seenInPath.has(id)) continue;
      seenInPath.add(id);
      const entry = highlightById.get(id);
      if (entry) {
        entry.pathIndexes.push(pathIndex);
      } else {
        // 首次命中即定色：重叠边取最先出现的那条路径的颜色。
        highlightById.set(id, { color, pathIndexes: [pathIndex] });
      }
    }
  });

  const highlightedEdges = edges.map((edge) => {
    const entry = highlightById.get(edge.id);
    if (!entry) return edge as PathHighlightedEdge<D>;
    return {
      ...edge,
      style: {
        ...edge.style,
        stroke: entry.color,
        strokeWidth: PATH_HIGHLIGHT_STROKE_WIDTH,
      },
      data: {
        ...edge.data,
        pathIndexes: [...entry.pathIndexes],
      } as D & LineagePathHighlightData,
    };
  });

  return { edges: highlightedEdges, pathSummaries };
}
