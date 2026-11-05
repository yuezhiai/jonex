/**
 * 血缘路径评分查询 API（P2b）—— 血缘画布「路径高亮」的数据源。
 *
 * 后端：`GET /api/v1/knowledge-base/ontology/lineage-paths`（网关 P2b Task 1）。
 * 与 ② NL→SQL 的路径搜索同源（同一份编译 schema 行、同一套边池与打分），
 * 画布上高亮的路就是模型实际会走的路。
 *
 * 语义边界（与 `capabilities/knowledge_base/services/ontology_lineage_paths.py` 一致）：
 * - `unknown=true` 只有一种含义：**该数据源没有 active 的编译 schema 行**
 *   （与「schema 在、但锚点不在图里」区分 —— 后者 `paths=[]`、`unknown=false`）；
 * - `explored` / `truncated` 原样来自后端路径搜索（后者 = 按分数取前 `max_paths`
 *   时有路径被砍掉）；
 * - 每个 step 的端点是**数据流方向**（`from_*` → `to_*`），`reversed` 单独标记
 *   遍历方向 —— 画布箭头/高亮按数据流端点映射，别拿遍历方向去反推边。
 *
 * ⚠️ `request` 是本仓的原始 axios 实例，响应仍是 `ApiResponse` 信封 —— 经 `getData`
 * 解包，错误不在这里吞：页面 catch 后原样透出后端 message（与 `api/ontologyDataAssets.ts`
 * 同口径）。
 */
import { request, getData } from './request';

const BASE = '/knowledge-base/ontology/lineage-paths';

/** 单跳类型：`join` 来自编译 schema，`lineage` 来自手工血缘边。 */
export type LineagePathStepType = 'join' | 'lineage';

/** 路径上的一跳（端点恒为数据流方向）。 */
export interface LineagePathStep {
  type: LineagePathStepType;
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  origin: string;
  /** 遍历时是否逆着数据流走（端点本身不翻转）。 */
  reversed: boolean;
}

/** 一条评分路径。 */
export interface LineagePath {
  score: number;
  /** 经过的 lineage 边数（join 边不计）。 */
  transfers: number;
  /** 是否命中调用方给的 `target`。 */
  hits_target: boolean;
  steps: LineagePathStep[];
}

/** `lineage-paths` 响应整体。 */
export interface LineagePathsResult {
  anchor: string;
  target: string | null;
  unknown: boolean;
  explored: number;
  truncated: boolean;
  paths: LineagePath[];
}

export interface LineagePathsQuery {
  /** 锚点物理表名（必填）。 */
  anchor: string;
  /** 可选目标表名（命中加分，`hits_target` 据此判定）。 */
  target?: string;
  /** 最大跳数（后端 ge=1, le=3）。 */
  maxHops?: number;
  /** 最多返回路径数（后端 ge=1, le=10）。 */
  maxPaths?: number;
  /** 最小跳数（后端 ge=1, le=3）。 */
  minHops?: number;
}

/**
 * 读取从锚点出发的评分路径。
 *
 * 缺省值在服务端（max_hops=3、max_paths=5、min_hops=1），这里只在调用方给了才下发 ——
 * 避免前端悄悄改写后端语义。响应缺失 `paths` 时兜底为空，但 `unknown`/`truncated`
 * 只认显式 `true`：缺省不是「未知」，也不是「截断」。
 */
export async function getLineagePaths(
  kbId: string,
  dsId: string,
  opts: LineagePathsQuery,
): Promise<LineagePathsResult> {
  const res = await getData<LineagePathsResult>(
    request.get(BASE, {
      params: {
        knowledge_base_id: kbId,
        data_source_id: dsId,
        anchor: opts.anchor,
        target: opts.target,
        max_hops: opts.maxHops,
        max_paths: opts.maxPaths,
        min_hops: opts.minHops,
      },
    }),
  );
  return {
    anchor: res?.anchor ?? opts.anchor,
    target: res?.target ?? null,
    unknown: res?.unknown === true,
    explored: res?.explored ?? 0,
    truncated: res?.truncated === true,
    paths: res?.paths ?? [],
  };
}
