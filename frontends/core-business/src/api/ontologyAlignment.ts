/**
 * 跨模态对齐（P1.5b）API —— KB 设置 →「概念对齐」页签的「数据资产对齐」区。
 *
 * 后端：`/api/v1/knowledge-base/ontology/data-alignment`（网关三个端点）。
 *
 * 口径（与姊妹模块 `api/ontologySynonym.ts` / `api/lineage.ts` 一致）：
 * - 响应是 `ApiResponse` 信封，一律经 `getData` / `postData` 解包；
 * - **`knowledge_base_id` 在 PATCH / GET 上都走 Query 且必填**（对齐行按 KB 收口，
 *   漏传不是「不过滤」，是网关 422）；PATCH 的 body 只带 `status`；
 * - 错误不在这一层吞：页面 catch 后原样显示后端 message（那是用户唯一能看懂
 *   失败原因的地方）。
 */
import { request, getData, postData } from './request';

const BASE = '/knowledge-base/ontology/data-alignment';

/** 对齐行状态白名单（与后端 `VALID_ALIGNMENT_STATUSES` 一一对应）。 */
export type AlignmentStatus = 'proposed' | 'active' | 'rejected';

/** 一条对齐记录（后端 `OntologyDataAlignment.to_dict()`，snake_case）。 */
export interface OntologyDataAlignment {
  id: string;
  knowledge_base_id?: string;
  /** 文档侧实体类型 */
  doc_type: string;
  data_source_id: string;
  /** 数据源侧实体名 */
  entity_name: string;
  status: AlignmentStatus;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  /** 读时判定：doc_type 已不在当前 KB 级 schema；null = 无法判定（missing/abstract） */
  stale?: boolean | null;
}

/** 对齐行列表回执：行 + KB 级 schema 的新鲜度（读时判定）。 */
export interface AlignmentListResult {
  items: OntologyDataAlignment[];
  freshness?: 'missing' | 'seeded' | 'outdated' | 'manual_edited' | 'abstract_projection';
}

/** 生成建议的回执：新增 / 已存在任意状态 / 一个可解析候选都没有的文档实体类型数。 */
export interface AlignmentSuggestionResult {
  created: number;
  existing: number;
  unresolved: number;
  /** KB 级 schema 的新鲜度（读时判定）。 */
  freshness?: string;
  /** `freshness == "outdated"`：需重编译，UI 显式提示。 */
  stale?: boolean;
  /** 机器可读原因；过期时为 `kb_schema_outdated`，否则 null。 */
  reason?: string | null;
}

/** 列该 KB 的对齐行；`status` 缺省 = 全状态（页面按状态分组展示）。 */
export async function listAlignments(
  kbId: string,
  status?: AlignmentStatus,
): Promise<AlignmentListResult> {
  const res = await getData<{ items?: OntologyDataAlignment[]; freshness?: string }>(
    request.get(BASE, { params: { knowledge_base_id: kbId, status } }),
  );
  return {
    items: res?.items ?? [],
    freshness: res?.freshness as AlignmentListResult['freshness'],
  };
}

/** 生成对齐建议（只落 `proposed`，幂等：已否过的不会重新提出）。 */
export async function suggestAlignments(kbId: string): Promise<AlignmentSuggestionResult> {
  const res = await postData<Partial<AlignmentSuggestionResult>>(`${BASE}/suggest`, {
    knowledge_base_id: kbId,
  });
  return {
    created: res?.created ?? 0,
    existing: res?.existing ?? 0,
    unresolved: res?.unresolved ?? 0,
    freshness: res?.freshness,
    stale: res?.stale,
    reason: res?.reason,
  };
}

/** 人审流转：proposed / active / rejected。返回更新后的整行。 */
export async function setAlignmentStatus(
  kbId: string,
  id: string,
  status: AlignmentStatus,
): Promise<OntologyDataAlignment> {
  return getData<OntologyDataAlignment>(
    request.patch(
      `${BASE}/${id}`,
      { status },
      { params: { knowledge_base_id: kbId } },
    ),
  );
}
