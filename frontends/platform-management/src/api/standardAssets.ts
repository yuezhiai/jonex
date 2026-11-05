/**
 * W2.2 标准资产（术语 / 数据元 / 指标口径卡）取数模块。
 *
 * 网关接口（`apiClient` 的 baseURL 已带 `/api/v1`）：
 *   GET    /data-governance/standard-assets
 *   GET    /data-governance/standard-assets/versions?kind&code
 *   GET    /data-governance/standard-assets/{asset_id}
 *   POST   /data-governance/standard-assets/drafts
 *   PUT    /data-governance/standard-assets/{asset_id}
 *   POST   /data-governance/standard-assets/{asset_id}/publish
 *   POST   /data-governance/standard-assets/{asset_id}/retire
 *   DELETE /data-governance/standard-assets/{asset_id}
 *
 * 约定：**后端 snake_case 只在本层出现**（行接口 + 映射 + 参数/body 组装都在
 * 本文件），页面与组件只接触 camelCase；`apiClient` 已解包
 * `success_response(data=...)`。
 */
import apiClient from './request';
import { DG_MAX_PAGE_SIZE } from '@/pages/DataGovernance/constant';
import { summarizeDraft } from '@/pages/DataGovernance/standardAssetUtils';
import type {
  DgStandardAssetDetail,
  DgStandardAssetDraftBody,
  DgStandardAssetItem,
  DgStandardAssetKind,
  DgStandardAssetListParams,
  DgStandardAssetListResult,
  DgStandardAssetStatus,
  DgStandardAssetUpdateBody,
} from '@/pages/DataGovernance/types';

const BASE = '/data-governance/standard-assets';

// ── 后端响应行（snake_case，仅本层可见） ─────────────────────────────

/**
 * 后端标准资产行（snake_case）。可空列用 `?` 兼容老响应/裁剪响应。
 *
 * 仅供本模块与单测使用；页面组件不得 import。
 */
export interface StandardAssetRowRaw {
  asset_id: string;
  kind: DgStandardAssetKind;
  code: string;
  name: string;
  version: number;
  status: DgStandardAssetStatus;
  revision: number;
  published_version?: number | null;
  draft_version?: number | null;
  has_draft?: boolean | null;
  updated_at?: string | null;
  updated_by?: string | null;
  /** 仅详情/版本接口返回；形状可信度低，映射时兜底成 `{}`。 */
  payload?: unknown;
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** 是不是可当作 payload 的对象（null / 数组 / 标量都不算）。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

// ── 纯函数：响应映射 ─────────────────────────────────────────────────

/** 列表行 snake_case → camelCase；可空字段缺失时给安全默认值。 */
export function mapStandardAssetItem(row: StandardAssetRowRaw): DgStandardAssetItem {
  const publishedVersion = numberOrNull(row.published_version);
  const draftVersion = numberOrNull(row.draft_version);
  return {
    assetId: row.asset_id,
    kind: row.kind,
    code: row.code,
    name: row.name,
    version: numberOr(row.version, 0),
    status: row.status,
    revision: numberOr(row.revision, 1),
    publishedVersion,
    draftVersion,
    // 后端在非管理员/未带 include_drafts 时会把 has_draft 强制为 false；
    // 字段缺失时才用版本关系兜底，避免把「不展示」误判成「有草稿」。
    hasDraft:
      typeof row.has_draft === 'boolean'
        ? row.has_draft
        : summarizeDraft({ draftVersion, publishedVersion }),
    updatedAt: row.updated_at ?? null,
    updatedBy: row.updated_by ?? null,
  };
}

/** 详情 = 列表字段 + payload（非对象一律回退空对象）。 */
export function mapStandardAssetDetail(
  row: StandardAssetRowRaw,
): DgStandardAssetDetail {
  return {
    ...mapStandardAssetItem(row),
    payload: isRecord(row.payload) ? row.payload : {},
  };
}

// ── 纯函数：请求参数 / body 组装 ─────────────────────────────────────

/** page 取整并夹到 ≥1（非有限数按 1 处理；无上限）。 */
function normalizePage(page: number): number {
  if (!Number.isFinite(page)) return 1;
  return Math.max(1, Math.trunc(page));
}

/** page_size 取整并夹到 1..DG_MAX_PAGE_SIZE（非有限数按 1 处理）。 */
function clampPageSize(pageSize: number): number {
  if (!Number.isFinite(pageSize)) return 1;
  return Math.min(DG_MAX_PAGE_SIZE, Math.max(1, Math.trunc(pageSize)));
}

/**
 * 列表参数 camelCase → 后端 snake_case。
 *
 * - 文本条件去空白，空则不传（后端按「有值才加条件」处理）；
 * - `include_drafts` 只在显式 `true` 时发送（默认 false，避免误传）；
 * - `page` 夹 ≥1，`page_size` 夹 1..100。
 */
export function buildStandardAssetListParams(
  params: DgStandardAssetListParams,
): Record<string, unknown> {
  const query: Record<string, unknown> = {
    page: normalizePage(params.page),
    page_size: clampPageSize(params.pageSize),
  };

  const textFields: Array<[string, string | undefined]> = [
    ['kind', params.kind],
    ['status', params.status],
    ['keyword', params.keyword],
  ];
  for (const [key, value] of textFields) {
    const trimmed = value?.trim();
    if (trimmed) query[key] = trimmed;
  }

  if (params.includeDrafts === true) {
    query.include_drafts = true;
  }
  return query;
}

/** 新建草稿 body：camelCase 入参 → snake_case；可选字段未传则不出现。 */
export function buildDraftBody(
  body: DgStandardAssetDraftBody,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    kind: body.kind,
    code: body.code,
    name: body.name,
  };
  if (body.payload !== undefined) payload.payload = body.payload;
  if (body.baseVersion !== undefined) payload.base_version = body.baseVersion;
  return payload;
}

/** 更新草稿 body：`expectedRevision` → `expected_revision`，name/payload 按需发送。 */
export function buildUpdateBody(
  body: DgStandardAssetUpdateBody,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    expected_revision: body.expectedRevision,
  };
  if (body.name !== undefined) payload.name = body.name;
  if (body.payload !== undefined) payload.payload = body.payload;
  return payload;
}

// ── 接口封装 ─────────────────────────────────────────────────────────

function assetPath(assetId: string): string {
  return `${BASE}/${encodeURIComponent(assetId)}`;
}

/** 列表；响应为 `{items, total, page, page_size}`，这里只消费前两者。 */
export async function listStandardAssets(
  params: DgStandardAssetListParams,
): Promise<DgStandardAssetListResult> {
  const data = await apiClient.get<{
    items?: StandardAssetRowRaw[];
    total?: number;
  }>(BASE, { params: buildStandardAssetListParams(params) });
  return {
    items: (data.items ?? []).map(mapStandardAssetItem),
    total: data.total ?? 0,
  };
}

export async function getStandardAsset(
  assetId: string,
): Promise<DgStandardAssetDetail> {
  const data = await apiClient.get<StandardAssetRowRaw>(assetPath(assetId));
  return mapStandardAssetDetail(data);
}

/** 同一 `kind + code` 的版本历史（倒序由后端保证）。 */
export async function listStandardAssetVersions(
  kind: DgStandardAssetKind,
  code: string,
): Promise<DgStandardAssetDetail[]> {
  const data = await apiClient.get<{ items?: StandardAssetRowRaw[] }>(
    `${BASE}/versions`,
    { params: { kind, code } },
  );
  return (data.items ?? []).map(mapStandardAssetDetail);
}

export async function createStandardDraft(
  body: DgStandardAssetDraftBody,
): Promise<DgStandardAssetDetail> {
  const data = await apiClient.post<StandardAssetRowRaw>(
    `${BASE}/drafts`,
    buildDraftBody(body),
  );
  return mapStandardAssetDetail(data);
}

export async function updateStandardDraft(
  assetId: string,
  body: DgStandardAssetUpdateBody,
): Promise<DgStandardAssetDetail> {
  const data = await apiClient.put<StandardAssetRowRaw>(
    assetPath(assetId),
    buildUpdateBody(body),
  );
  return mapStandardAssetDetail(data);
}

export async function publishStandardAsset(
  assetId: string,
  expectedRevision: number,
): Promise<DgStandardAssetDetail> {
  const data = await apiClient.post<StandardAssetRowRaw>(
    `${assetPath(assetId)}/publish`,
    { expected_revision: expectedRevision },
  );
  return mapStandardAssetDetail(data);
}

export async function retireStandardAsset(
  assetId: string,
): Promise<DgStandardAssetDetail> {
  const data = await apiClient.post<StandardAssetRowRaw>(
    `${assetPath(assetId)}/retire`,
  );
  return mapStandardAssetDetail(data);
}

/** 删除草稿（仅 `status=draft` 的行可删；其余后端回冲突）。 */
export async function deleteStandardDraft(assetId: string): Promise<void> {
  await apiClient.delete(assetPath(assetId));
}
