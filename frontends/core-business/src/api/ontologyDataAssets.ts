/**
 * 结构化数据资产子图 API —— 血缘页签的画布视图（P2a）。
 *
 * 后端：`GET /api/v1/knowledge-base/ontology/data-assets`（网关 Task 1 的查询端点）。
 *
 * 语义边界（与后端 `ontology_data_assets.filter_neighbors` 一致）：
 * - 不传 `table` = 该源**整图**节点/边（受画布上限约束，超限时 `truncated=true`）；
 *   血缘画布用它取**锚点候选** —— join-only 源没有手工血缘边，候选必须来自全图节点；
 * - `direction`：`downstream` 沿 from→to、`upstream` 沿 to→from、`both` 取并集；
 * - `hops` 由后端收敛到 1..3；
 * - `unknown=true` = **这张表不在该源的数据资产投影里**（与「表在、但没边」区分）；
 * - `truncated=true` = 节点/边超过画布上限（200/400）已截断。
 *
 * ⚠️ `request` 是本仓的原始 axios 实例，响应仍是 `ApiResponse` 信封 —— 经 `getData`
 * 解包，错误不在这里吞：页面 catch 后原样透出后端 message（与 `api/lineage.ts` 同口径）。
 *
 * 线格式类型（`DataAssetNode/Edge/Graph`）以本文件为**唯一定义处**：画布图模型
 * （`lineageGraphModel.ts`）只 import type 再转出，避免两处形状漂移。
 */
import { request, getData } from './request';

const BASE = '/knowledge-base/ontology/data-assets';

/** 投影里的一个表节点（`attributes` 已被后端还原为列表）。 */
export interface DataAssetNode {
  table: string;
  entity_name?: string;
  attributes?: unknown[];
  schema_version?: number;
  /** 投影来源（当前恒为 `compiled_schema`；保留字段供将来多种投影来源区分） */
  provenance?: string;
}

/** 列映射项：来源列 → 去向列（后端按表对聚合后给出）。 */
export interface DataAssetColumnMapEntry {
  from_column: string;
  to_column: string;
}

/** 一条表级血缘边（`column_map` 为列级明细）。 */
export interface DataAssetEdge {
  from_table: string;
  to_table: string;
  column_map?: DataAssetColumnMapEntry[];
  origin?: string;
  confidence?: number;
}

/** `data-assets` 响应整体。 */
export interface DataAssetGraph {
  nodes: DataAssetNode[];
  edges: DataAssetEdge[];
  unknown: boolean;
  truncated: boolean;
}

export interface DataAssetQuery {
  /**
   * 锚点表（物理表名）。不传 = 整源快照（全图，超限截断）；
   * 画布用这种模式取锚点候选，带 `table` 的模式取锚点 BFS 子图。
   */
  table?: string;
  direction?: 'upstream' | 'downstream' | 'both';
  /** BFS 跳数（后端 ge=1, le=3）。 */
  hops?: number;
}

/**
 * 读取数据资产子图。
 *
 * 缺省值在服务端（direction=both、hops=1），这里只在调用方给了才下发 ——
 * 避免前端悄悄改写后端语义。响应缺失数组时兜底为空，但 `unknown`/`truncated`
 * 只认显式 `true`：缺省不是「未知」，也不是「截断」。
 */
export async function getDataAssets(
  kbId: string,
  dsId: string,
  opts: DataAssetQuery = {},
): Promise<DataAssetGraph> {
  const res = await getData<DataAssetGraph>(
    request.get(BASE, {
      params: {
        knowledge_base_id: kbId,
        data_source_id: dsId,
        table: opts.table,
        direction: opts.direction,
        hops: opts.hops,
      },
    }),
  );
  return {
    nodes: res?.nodes ?? [],
    edges: res?.edges ?? [],
    unknown: res?.unknown === true,
    truncated: res?.truncated === true,
  };
}
