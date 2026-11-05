/**
 * 血缘画布图模型（纯函数，node 环境可测，不依赖 React / DOM）。
 *
 * 输入是 `GET /knowledge-base/ontology/data-assets` 的响应，输出是 React Flow
 * 的节点/边。**布局在模型层一次算好**（与本体画布一致）：画布组件只负责渲染，
 * 不做第二套坐标计算。
 *
 * 方向语义：锚点深度 0，上游为负、下游为正 —— 上游在左、下游在右。
 * `x = (depth - minDepth) * 300`（最小深度归零），同层按表名字典序
 * `y = 序号 * 110`。行距/列距都大于节点尺寸（240×80），层与行不会重叠。
 *
 * 深度 = 从锚点出发的**有向带符号 BFS**（`from→to` 记 +1、反向记 -1）。
 * 节点首次出队时定深度、之后不再回改：深度只由「离锚点的跳数」决定，
 * 与响应里边的返回顺序无关（逐边松弛会让交叉边把直连邻居定成 0、把链
 * 叠到 k 跳开外）。环安全：已访问节点不再入队，遍历必然终止。
 *
 * 悬空边：Task 1 的节点上限（200）与边上限（400）**独立生效**，响应里可能
 * 出现端点被节点上限截掉、边却还在的组合。这种边在画布上画不出来，也会
 * 污染深度传播，必须在布局前丢弃。
 */
import type { Edge, Node } from '@xyflow/react';

import type {
  DataAssetColumnMapEntry,
  DataAssetEdge,
  DataAssetGraph,
  DataAssetNode,
} from '@/api/ontologyDataAssets';

// 线格式类型以 API 客户端为唯一定义处（本模块只消费并转出），避免两处形状漂移。
// `import type` 在运行期被擦除，本模块仍是纯函数模块（node 环境可测）。
export type { DataAssetColumnMapEntry, DataAssetEdge, DataAssetGraph, DataAssetNode };

/** 画布节点携带的业务字段（Task 3 的 `lineageTable` 节点组件从这里取）。 */
export interface LineageNodeData extends Record<string, unknown> {
  table: string;
  entityName?: string;
  attributeCount: number;
  /** 相对锚点的层：0 = 锚点，负 = 上游，正 = 下游。 */
  depth: number;
}

/** 画布节点；`type` 固定为自定义节点 `lineageTable`。 */
export type LineageRFNode = Node<LineageNodeData, 'lineageTable'>;

/** 画布边携带的业务字段。 */
export interface LineageEdgeData extends Record<string, unknown> {
  origin?: string;
  confidence?: number;
  columnCount: number;
  columnMap: DataAssetColumnMapEntry[];
}

/** 画布边（React Flow 内置边样式，颜色由 `origin` 决定）。 */
export type LineageRFEdge = Edge<LineageEdgeData>;

export interface LineageGraphModel {
  nodes: LineageRFNode[];
  edges: LineageRFEdge[];
  unknown: boolean;
  truncated: boolean;
}

/** 自定义节点尺寸；行距/列距（110 / 300）都留出了余量。 */
export const LINEAGE_NODE_WIDTH = 240;
export const LINEAGE_NODE_HEIGHT = 80;

/** 层间距 / 同层行距。 */
const X_STEP = 300;
const Y_STEP = 110;

/**
 * `origin` 档 → 边色。与列表页 `LineageTab` 的色板同源：
 * declared 绿、manual 蓝（AntD blue）、comment 黄、platform 紫、value 青、derived 灰。
 * 未知档位（后端将来新增）与缺失 origin 一律按灰处理 —— 不猜、也不吞。
 */
export const LINEAGE_ORIGIN_COLORS: Readonly<Record<string, string>> = {
  declared: '#52c41a',
  manual: '#1677ff',
  comment: '#faad14',
  platform: '#722ed1',
  value: '#13c2c2',
  derived: '#8c8c8c',
};

const FALLBACK_EDGE_COLOR = '#8c8c8c';

/** 边色（含未知/缺省 origin 的灰色兜底）。 */
export function lineageEdgeColor(origin?: string): string {
  return (origin && LINEAGE_ORIGIN_COLORS[origin]) || FALLBACK_EDGE_COLOR;
}

/**
 * 置信度 → 边线宽：`1 + round(confidence * 2)`，下限 1。
 * 即缺省/0 → 1、0.5 → 2、1 → 3。置信度是**视觉编码**（图例「线宽 = 置信度」），
 * 未知/缺失按最低档处理 —— 与 origin 的灰色兜底同口径：不猜、不虚构置信度。
 */
export function lineageEdgeWidth(confidence?: number): number {
  return Math.max(1, 1 + Math.round((confidence ?? 0) * 2));
}

/**
 * 画布边 id 的唯一口径：`from->to`（**数据流方向**）。
 * 边创建（`buildLineageGraph`）与路径高亮匹配（`applyPathHighlights`）都走这里，
 * 避免两处各拼一套格式后静默失配。
 */
export function lineageEdgeId(fromTable: string, toTable: string): string {
  return `${fromTable}->${toTable}`;
}

/** 表名字典序；用码点比较而非 `localeCompare`，跨运行环境结果稳定。 */
function compareTable(a: DataAssetNode, b: DataAssetNode): number {
  if (a.table < b.table) return -1;
  if (a.table > b.table) return 1;
  return 0;
}

/**
 * 画布锚点候选：当前源已取到的血缘边里出现过的**全部表名**（from 与 to 都算）。
 *
 * 去重后按码点排序 —— 候选顺序不跟随后端边的返回顺序，切源/刷新时下拉不会跳。
 * 入参只要求 `from_table`/`to_table` 两个字段：既接受 `LineageEdge`，也接受
 * `DataAssetEdge`（两处都能给画布供锚点候选）。
 */
export function anchorTables(
  edges: ReadonlyArray<{ from_table: string; to_table: string }>,
): string[] {
  const tables = new Set<string>();
  for (const edge of edges) {
    tables.add(edge.from_table);
    tables.add(edge.to_table);
  }
  return [...tables].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

/** `lineageRevision` 的最小入参：表对 + 两条时间戳（`LineageEdge` 直接可传）。 */
export interface RevisionEdgeLike {
  from_table: string;
  to_table: string;
  created_at?: string | null;
  updated_at?: string | null;
}

/**
 * 边集内容修订号。画布的锚点候选只看 `anchorTables(edges)`（表集合），
 * **同表对新增第二条列级边时表集合不变** —— 只靠表集判重查会让画布停在旧
 * `columnCount`（Task 4 验收场景）。这里把「边数 + 最近时间戳 + 表集」拼成
 * 内容签名，父级每次 reload 再叠加序号（页头「刷新」即会推进）。
 */
export function lineageRevision(edges: ReadonlyArray<RevisionEdgeLike>): string {
  let latest = '';
  for (const edge of edges) {
    const ts = edge.updated_at ?? edge.created_at ?? '';
    if (ts > latest) latest = ts;
  }
  return `${edges.length}:${latest}:${anchorTables(edges).join('|')}`;
}

/**
 * 响应 → 画布模型。
 *
 * - `data.unknown` 或锚点不在 `nodes` 里 ⇒ 空图 + `unknown=true`（「表不在投影里」
 *   是显式结论，不能渲染成「没有上下游」）；`truncated` 始终原样透传。
 * - 锚点存在但没有任何边 ⇒ 单节点图 + `unknown=false`（表在、只是没边）。
 * - 与锚点不连通的节点不上画布（正常响应是锚点 BFS 子图，这里只做防御）。
 */
export function buildLineageGraph(data: DataAssetGraph, anchor: string): LineageGraphModel {
  const empty = (unknown: boolean): LineageGraphModel => ({
    nodes: [],
    edges: [],
    unknown,
    truncated: data.truncated,
  });

  if (data.unknown) return empty(true);

  const nodeByTable = new Map<string, DataAssetNode>();
  for (const n of data.nodes) nodeByTable.set(n.table, n);
  if (!nodeByTable.has(anchor)) return empty(true);

  // 悬空边先剔除：端点不在节点集合里的边既不画，也不能参与深度传播。
  const connectedEdges = data.edges.filter(
    (e) => nodeByTable.has(e.from_table) && nodeByTable.has(e.to_table),
  );

  // 有向带符号邻接表：from→to 时 to 相对 from +1，反向时 from 相对 to -1。
  // BFS 从锚点（深度 0）出发，节点首次出队时定深度、已访问不再入队 ——
  // 直连邻居恒为 ±1、链尾按跳数递增，结果与边的返回顺序无关。
  const adjacency = new Map<string, Array<{ neighbor: string; delta: number }>>();
  const connect = (from: string, to: string, delta: number) => {
    const list = adjacency.get(from);
    if (list) list.push({ neighbor: to, delta });
    else adjacency.set(from, [{ neighbor: to, delta }]);
  };
  for (const e of connectedEdges) {
    connect(e.from_table, e.to_table, 1);
    connect(e.to_table, e.from_table, -1);
  }

  const depths = new Map<string, number>([[anchor, 0]]);
  const visited = new Set<string>([anchor]);
  // 队列项携带「父节点 + 增量」，出队时才定深度；同一节点可能被多个父节点
  // 入队（菱形/多条列级边），首次出队胜出，其余重复项直接跳过。
  const queue: Array<{ table: string; from: string; delta: number }> = [];
  for (const { neighbor, delta } of adjacency.get(anchor) ?? []) {
    queue.push({ table: neighbor, from: anchor, delta });
  }
  for (let head = 0; head < queue.length; head += 1) {
    const { table, from, delta } = queue[head];
    if (visited.has(table)) continue;
    visited.add(table);
    depths.set(table, (depths.get(from) as number) + delta);
    for (const next of adjacency.get(table) ?? []) {
      if (!visited.has(next.neighbor)) {
        queue.push({ table: next.neighbor, from: table, delta: next.delta });
      }
    }
  }

  // 未到达锚点的节点没有深度：不上画布，相关边随之一并丢弃。
  const reached = data.nodes.filter((n) => depths.has(n.table));

  const byDepth = new Map<number, DataAssetNode[]>();
  for (const n of reached) {
    const depth = depths.get(n.table) as number;
    const group = byDepth.get(depth);
    if (group) group.push(n);
    else byDepth.set(depth, [n]);
  }
  const depthKeys = [...byDepth.keys()].sort((a, b) => a - b);
  // `reached` 至少包含锚点，所以 minDepth 一定存在。
  const minDepth = depthKeys[0];

  const nodes: LineageRFNode[] = [];
  for (const depth of depthKeys) {
    const group = byDepth.get(depth) as DataAssetNode[];
    group.sort(compareTable);
    group.forEach((n, index) => {
      nodes.push({
        id: n.table,
        type: 'lineageTable',
        position: { x: (depth - minDepth) * X_STEP, y: index * Y_STEP },
        data: {
          table: n.table,
          entityName: n.entity_name,
          attributeCount: Array.isArray(n.attributes) ? n.attributes.length : 0,
          depth,
        },
      });
    });
  }

  const edges: LineageRFEdge[] = [];
  for (const e of connectedEdges) {
    // 两端都到达锚点（有深度）才画线；与锚点不连通的分量整条丢弃。
    if (!depths.has(e.from_table) || !depths.has(e.to_table)) continue;
    const columnMap = e.column_map ?? [];
    const columnCount = columnMap.length;
    const color = lineageEdgeColor(e.origin);
    edges.push({
      id: lineageEdgeId(e.from_table, e.to_table),
      source: e.from_table,
      target: e.to_table,
      label: String(columnCount),
      // 线宽编码置信度（图例「线宽 = 置信度」）：模型层算一次，组件只渲染。
      style: { stroke: color, strokeWidth: lineageEdgeWidth(e.confidence) },
      labelStyle: { fill: color },
      data: {
        origin: e.origin,
        confidence: e.confidence,
        columnCount,
        columnMap,
      },
    });
  }

  return {
    nodes,
    edges,
    unknown: false,
    truncated: data.truncated,
  };
}
