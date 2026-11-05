/**
 * 血缘边（列级）API —— KB 设置 →「血缘」页签。
 *
 * 后端：`/api/v1/knowledge-base/lineage-edges`（网关 Task 4 的四个端点）。
 *
 * 两条与姊妹模块 `api/ontologySynonym.ts` 不同的口径，**不要「顺手统一」**：
 *
 * 1. **逐条 CRUD，全程不产生 409**：重复边是 1001/400、边不存在是 1002/404。
 *    前端**不要**照 `AbstractSchemaTab` 那样把 `409 || bizCode === 1003` 读成
 *    「覆盖确认」—— 血缘边没有「整份覆盖」语义（设计硬约束）。
 * 2. **update / delete 的 `knowledge_base_id` 走 Query（必填）**：边按 KB 收口
 *    （审查 M-8：只按 edge_id 定位能让同租户的另一个 KB 的边被改/删）。
 *    网关对该参数带 `min_length=1` —— 漏传不是「不过滤」，是请求直接 422。
 *
 * ⚠️ `request` 是本仓的**原始 axios 实例**（`api/request.ts` 导出的是 `api.raw`），
 * 响应仍是 `ApiResponse` 信封，所以一律经 `getData` / `postData` 解包 ——
 * 与 `api/ontologySynonym.ts` 同口径。错误不在这里吞：页面 catch 原样透出后端 message
 * （400 的「表 X 上没有列 Y」「该血缘边已存在」是用户唯一能看懂失败原因的地方）。
 */
import { request, getData, postData } from './request';

const BASE = '/knowledge-base/lineage-edges';

/** 一条列级血缘边（后端 `LineageEdge.to_dict()` 的形状，snake_case）。 */
export interface LineageEdge {
  id: string;
  /** 边按源收口：表名是物理表名，跨源没有意义 */
  data_source_id: string;
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  /** declared | manual | comment | platform | value | derived */
  origin: string;
  note?: string | null;
  /** 来源最后一次确认该边的时间（迁移 012；人工编辑/导入/OpenLineage 均刷新） */
  last_seen_at?: string | null;
  /** 读时计算：`origin=platform` 且超过失效窗口（默认 30 天）未再确认 */
  stale?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  /** 后端也回带（对外不泄漏 tenant_id，但 KB 与创建人都在） */
  knowledge_base_id?: string;
  created_by?: string | null;
}

export interface LineageEdgeList {
  items: LineageEdge[];
  total: number;
}

/**
 * 编辑载荷。`origin` 不在其中：后端一律压成 `manual`（档位是可信度声明，UI 不许自选）。
 *
 * ⚠️ **不含 `data_source_id` 是故意的**：边不能换源 —— 后端 `update()` 见到不同的源会
 * 直接 400（「血缘边不能更换数据源（请删除后在新源上重建）」）。类型层面挡住比运行时挡住好。
 */
export interface LineageEdgeInput {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  note?: string;
}

/**
 * 新建载荷：**`data_source_id` 必填**。
 *
 * 后端 `lineage_edge_service.create()` 缺它直接 1001/400（表名是物理表名，必须指明是哪个源）。
 * `knowledge_base_id` 不在载荷里 —— 走函数签名的第一个参数（与 update/delete 同款）。
 */
export interface LineageEdgeCreateInput extends LineageEdgeInput {
  data_source_id: string;
}

/** 列某数据源的血缘边；不传 `dataSourceId` = 该 KB 全源。 */
export async function listLineageEdges(
  knowledgeBaseId: string,
  dataSourceId?: string,
): Promise<LineageEdgeList> {
  const res = await getData<LineageEdgeList>(
    request.get(BASE, {
      params: { knowledge_base_id: knowledgeBaseId, data_source_id: dataSourceId },
    }),
  );
  return { items: res?.items ?? [], total: res?.total ?? 0 };
}

/**
 * 新增一条边。
 *
 * 签名与 update/delete 同款：**`knowledge_base_id` 走第一个参数**，由函数注入 body。
 * `data_source_id` 由调用方经 `body` 带入（面板当前的源），**不给用户选** ——
 * 表名是物理表名，选错源等于把边写到一张不存在的表上。
 */
export async function createLineageEdge(
  knowledgeBaseId: string,
  body: LineageEdgeCreateInput,
): Promise<LineageEdge> {
  return postData<LineageEdge>(BASE, { ...body, knowledge_base_id: knowledgeBaseId });
}

/**
 * 更新一条边。
 *
 * ⚠️ `knowledgeBaseId` 必须走 **Query**（网关必填、`min_length=1`）——
 * 这是「按 KB 收口」的那道过滤（M-8）；只放进 body 过不了网关的参数校验。
 */
export async function updateLineageEdge(
  knowledgeBaseId: string,
  edgeId: string,
  body: LineageEdgeInput,
): Promise<LineageEdge> {
  return getData<LineageEdge>(
    request.patch(`${BASE}/${edgeId}`, body, {
      params: { knowledge_base_id: knowledgeBaseId },
    }),
  );
}

/** 删除一条边（软删）。后端回 `{ deleted: true }`；`knowledgeBaseId` 同 update 走 Query。 */
export async function deleteLineageEdge(
  knowledgeBaseId: string,
  edgeId: string,
): Promise<boolean> {
  const res = await getData<{ deleted: boolean }>(
    request.delete(`${BASE}/${edgeId}`, {
      params: { knowledge_base_id: knowledgeBaseId },
    }),
  );
  return res?.deleted === true;
}

// ── 血缘「禁止同查」表对（roadmap #10）────────────────────────────────────

const FORBIDDEN_BASE = '/knowledge-base/lineage-forbidden-pairs';

export interface LineageForbiddenPair {
  id: string;
  knowledge_base_id?: string;
  data_source_id: string;
  table_a: string;
  table_b: string;
  /** manual | derived */
  origin: string;
  /** proposed | active | disabled */
  status: string;
  note?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface LineageForbiddenPairList {
  items: LineageForbiddenPair[];
  total: number;
}

export async function listLineageForbiddenPairs(
  knowledgeBaseId: string,
  dataSourceId?: string,
  status?: string,
): Promise<LineageForbiddenPairList> {
  const res = await getData<LineageForbiddenPairList>(
    request.get(FORBIDDEN_BASE, {
      params: { knowledge_base_id: knowledgeBaseId, data_source_id: dataSourceId, status },
    }),
  );
  return { items: res?.items ?? [], total: res?.total ?? 0 };
}

export async function createLineageForbiddenPair(
  knowledgeBaseId: string,
  body: { data_source_id: string; table_a: string; table_b: string; note?: string },
): Promise<LineageForbiddenPair> {
  return postData<LineageForbiddenPair>(FORBIDDEN_BASE, {
    ...body, knowledge_base_id: knowledgeBaseId,
  });
}

export async function updateLineageForbiddenPair(
  knowledgeBaseId: string,
  pairId: string,
  body: { status?: string; note?: string },
): Promise<LineageForbiddenPair> {
  return getData<LineageForbiddenPair>(
    request.patch(`${FORBIDDEN_BASE}/${pairId}`, body, {
      params: { knowledge_base_id: knowledgeBaseId },
    }),
  );
}

export async function deleteLineageForbiddenPair(
  knowledgeBaseId: string,
  pairId: string,
): Promise<boolean> {
  const res = await getData<{ deleted: boolean }>(
    request.delete(`${FORBIDDEN_BASE}/${pairId}`, {
      params: { knowledge_base_id: knowledgeBaseId },
    }),
  );
  return res?.deleted === true;
}

export async function suggestLineageForbiddenPairs(
  knowledgeBaseId: string,
  dataSourceId: string,
): Promise<{ created: number; skipped: number; total: number }> {
  return postData(
    `/knowledge-base/data-sources/${dataSourceId}/lineage-forbidden-suggest`,
    { knowledge_base_id: knowledgeBaseId },
  );
}
