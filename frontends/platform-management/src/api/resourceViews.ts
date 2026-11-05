/**
 * W2.6 资源视图取数模块（业务域 / 责任 / 落标 / 原文锚点）。
 *
 * 约定与 W2.1/W2.2/W2.5 一致：后端 snake_case **只在本层出现**，映射与
 * 查询参数组装都是纯函数；页面与组件只接触 camelCase。`apiClient` 已解包
 * `success_response(data=...)`，因此这里拿到的是业务数据本身。
 */
import type {
  DgBindingCreateBody,
  DgBindingListParams,
  DgBindingListResult,
  DgBindingSuggestion,
  DgBindingSuggestionParams,
  DgBindingUpdateBody,
  DgCoverageParams,
  DgCoverageResult,
  DgCoverageRow,
  DgDomainViewGroup,
  DgDomainViewParams,
  DgDomainViewResult,
  DgDomainViewTotals,
  DgResourceAssetCounts,
  DgResourceOpenIssues,
  DgResourceOwnerCount,
  DgResourceSensitivity,
  DgResponsibilityParams,
  DgResponsibilityResult,
  DgResponsibilityRow,
  DgResponsibilityTotals,
  DgSourceAnchorLink,
  DgSourceAnchorResult,
  DgStandardBinding,
} from '@/pages/DataGovernance/types';
import apiClient from './request';

const BASE = '/data-governance/resource-views';

// ── 原始响应形状（snake_case） ───────────────────────────────────────

interface DomainGroupRaw {
  space_id?: unknown;
  space_name?: unknown;
  domain?: unknown;
  asset_counts?: unknown;
  owner_count?: unknown;
  owners?: unknown;
  sensitivity?: unknown;
  open_issues?: unknown;
}

interface ResponsibilityRowRaw {
  key?: unknown;
  display_name?: unknown;
  asset_counts?: unknown;
  owner_count?: unknown;
  sensitivity?: unknown;
  open_issues?: unknown;
  last_updated_at?: unknown;
}

interface BindingRowRaw {
  id?: unknown;
  tenant_id?: unknown;
  standard_kind?: unknown;
  standard_code?: unknown;
  standard_asset_id?: unknown;
  asset_type?: unknown;
  asset_id?: unknown;
  source?: unknown;
  confidence?: unknown;
  note?: unknown;
  status?: unknown;
  revision?: unknown;
  created_by?: unknown;
  updated_by?: unknown;
  created_at?: unknown;
  updated_at?: unknown;
}

interface CoverageRowRaw {
  standard_asset_id?: unknown;
  standard_kind?: unknown;
  standard_code?: unknown;
  name?: unknown;
  version?: unknown;
  domain?: unknown;
  status?: unknown;
  bindings_count?: unknown;
  bound_assets?: unknown;
  unbound?: unknown;
}

interface SuggestionRowRaw {
  standard_kind?: unknown;
  standard_code?: unknown;
  standard_name?: unknown;
  name?: unknown;
  asset_type?: unknown;
  asset_id?: unknown;
  asset_name?: unknown;
  confidence?: unknown;
  reason?: unknown;
}

// ── 小工具（null / 脏值容错） ────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function recordOrEmpty(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function listOrEmpty(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function numberOr(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function textOr(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function boolOrFalse(value: unknown): boolean {
  return value === true;
}

function mapAssetCounts(value: unknown): DgResourceAssetCounts {
  const row = recordOrEmpty(value);
  return {
    table: numberOr(row.table),
    column: numberOr(row.column),
    metric: numberOr(row.metric),
    document: numberOr(row.document),
    dataSource: numberOr(row.data_source),
  };
}

function mapSensitivity(value: unknown): DgResourceSensitivity {
  const row = recordOrEmpty(value);
  return {
    public: numberOr(row.public),
    internal: numberOr(row.internal),
    confidential: numberOr(row.confidential),
    secret: numberOr(row.secret),
    unclassified: numberOr(row.unclassified),
  };
}

function mapOpenIssues(value: unknown): DgResourceOpenIssues {
  const row = recordOrEmpty(value);
  return {
    critical: numberOr(row.critical),
    error: numberOr(row.error),
    warning: numberOr(row.warning),
    info: numberOr(row.info),
  };
}

function mapOwners(value: unknown): DgResourceOwnerCount[] {
  return listOrEmpty(value).map((item) => {
    const row = recordOrEmpty(item);
    return { owner: textOr(row.owner), count: numberOr(row.count) };
  });
}

/** 绑定资产类型：只认 table/column/metric，未知值保守归为 table。 */
function bindingAssetType(value: unknown, fallback: 'table' | 'column' | 'metric' = 'table') {
  return value === 'table' || value === 'column' || value === 'metric'
    ? value
    : fallback;
}

// ── 纯映射 ───────────────────────────────────────────────────────────

export function mapDomainGroup(raw: DomainGroupRaw): DgDomainViewGroup {
  return {
    spaceId: stringOrNull(raw.space_id),
    spaceName: stringOrNull(raw.space_name),
    domain: textOr(raw.domain),
    assetCounts: mapAssetCounts(raw.asset_counts),
    ownerCount: numberOr(raw.owner_count),
    owners: mapOwners(raw.owners),
    sensitivity: mapSensitivity(raw.sensitivity),
    openIssues: mapOpenIssues(raw.open_issues),
  };
}

function mapDomainTotals(raw: unknown): DgDomainViewTotals {
  const row = recordOrEmpty(raw);
  return {
    assetCounts: mapAssetCounts(row.asset_counts),
    ownerCount: numberOr(row.owner_count),
    owners: mapOwners(row.owners),
    sensitivity: mapSensitivity(row.sensitivity),
    openIssues: mapOpenIssues(row.open_issues),
    groups: numberOr(row.groups),
  };
}

export function mapResponsibilityRow(raw: ResponsibilityRowRaw): DgResponsibilityRow {
  const row: DgResponsibilityRow = {
    key: textOr(raw.key),
    displayName: textOr(raw.display_name),
    assetCounts: mapAssetCounts(raw.asset_counts),
    sensitivity: mapSensitivity(raw.sensitivity),
    openIssues: mapOpenIssues(raw.open_issues),
    lastUpdatedAt: stringOrNull(raw.last_updated_at),
  };
  // 后端 domain 分组行不带 owner_count；缺失时保持「未知」，不伪造 0。
  if (raw.owner_count !== undefined && raw.owner_count !== null) {
    row.ownerCount = numberOr(raw.owner_count);
  }
  return row;
}

function mapResponsibilityTotals(raw: unknown): DgResponsibilityTotals {
  const row = recordOrEmpty(raw);
  return {
    assetCounts: mapAssetCounts(row.asset_counts),
    ownerCount: numberOr(row.owner_count),
    owners: mapOwners(row.owners),
    sensitivity: mapSensitivity(row.sensitivity),
    openIssues: mapOpenIssues(row.open_issues),
    groups: numberOr(row.groups),
    unowned: numberOr(row.unowned),
    unassignedDomain: numberOr(row.unassigned_domain),
  };
}

export function mapBinding(raw: BindingRowRaw): DgStandardBinding {
  return {
    id: textOr(raw.id),
    tenantId: textOr(raw.tenant_id),
    standardKind: textOr(raw.standard_kind) as DgStandardBinding['standardKind'],
    standardCode: textOr(raw.standard_code),
    standardAssetId: stringOrNull(raw.standard_asset_id),
    assetType: bindingAssetType(raw.asset_type),
    assetId: textOr(raw.asset_id),
    source: raw.source === 'suggested' ? 'suggested' : 'manual',
    confidence: numberOrNull(raw.confidence),
    note: stringOrNull(raw.note),
    status: raw.status === 'disabled' ? 'disabled' : 'active',
    revision: numberOr(raw.revision, 1),
    createdBy: stringOrNull(raw.created_by),
    updatedBy: stringOrNull(raw.updated_by),
    createdAt: stringOrNull(raw.created_at),
    updatedAt: stringOrNull(raw.updated_at),
  };
}

export function mapCoverageRow(raw: CoverageRowRaw): DgCoverageRow {
  const bindingsCount = numberOr(raw.bindings_count);
  return {
    standardAssetId: textOr(raw.standard_asset_id),
    standardKind: textOr(raw.standard_kind) as DgCoverageRow['standardKind'],
    standardCode: textOr(raw.standard_code),
    name: textOr(raw.name),
    version: numberOr(raw.version),
    domain: stringOrNull(raw.domain),
    status: textOr(raw.status),
    bindingsCount,
    boundAssets: listOrEmpty(raw.bound_assets).map((item) => {
      const row = recordOrEmpty(item);
      return {
        bindingId: textOr(row.binding_id),
        assetType: bindingAssetType(row.asset_type),
        assetId: textOr(row.asset_id),
        source: row.source === 'suggested' ? 'suggested' : 'manual',
        confidence: numberOrNull(row.confidence),
      };
    }),
    // 后端显式给 unbound；脏数据缺失时按绑定数推导，避免把已绑定标成未落标。
    unbound: raw.unbound === undefined || raw.unbound === null
      ? bindingsCount === 0
      : boolOrFalse(raw.unbound),
  };
}

export function mapSuggestion(raw: SuggestionRowRaw): DgBindingSuggestion {
  const standardName = textOr(raw.standard_name) || textOr(raw.name);
  return {
    standardKind: textOr(raw.standard_kind) as DgBindingSuggestion['standardKind'],
    standardCode: textOr(raw.standard_code),
    standardName,
    name: textOr(raw.name) || standardName,
    // 建议只覆盖列 / 指标；未知类型保守归为 column。
    assetType: bindingAssetType(raw.asset_type, 'column'),
    assetId: textOr(raw.asset_id),
    assetName: textOr(raw.asset_name),
    confidence: numberOrNull(raw.confidence),
    reason: stringOrNull(raw.reason),
  };
}

export function mapAnchorResult(raw: unknown): DgSourceAnchorResult {
  const row = recordOrEmpty(raw);
  const asset = recordOrEmpty(row.asset);
  return {
    asset: {
      assetType: (textOr(asset.asset_type) || 'table') as DgSourceAnchorResult['asset']['assetType'],
      assetId: textOr(asset.asset_id),
    },
    linked: listOrEmpty(row.linked).map((item): DgSourceAnchorLink => {
      const link = recordOrEmpty(item);
      return {
        knowledgeBaseId: textOr(link.knowledge_base_id),
        docType: textOr(link.doc_type),
        entityName: textOr(link.entity_name),
        entityCount: numberOr(link.entity_count),
        truncated: boolOrFalse(link.truncated),
        attributes: recordOrEmpty(link.attributes),
        documents: listOrEmpty(link.documents).map((doc) => {
          const document = recordOrEmpty(doc);
          return {
            id: textOr(document.id),
            name: textOr(document.name),
            status: textOr(document.status),
          };
        }),
      };
    }),
    unresolved: boolOrFalse(row.unresolved),
    degraded: boolOrFalse(row.degraded),
  };
}

// ── 查询参数 / 请求体组装（camelCase → snake_case） ──────────────────

export function buildDomainViewParams(
  params: DgDomainViewParams,
): Record<string, unknown> {
  return {
    ...(params.spaceId ? { space_id: params.spaceId } : {}),
    ...(params.domain ? { domain: params.domain } : {}),
  };
}

export function buildResponsibilityParams(
  params: DgResponsibilityParams,
): Record<string, unknown> {
  return {
    group_by: params.groupBy || 'owner',
    ...(params.spaceId ? { space_id: params.spaceId } : {}),
  };
}

export function buildBindingListParams(
  params: DgBindingListParams,
): Record<string, unknown> {
  return {
    ...(params.standardKind ? { standard_kind: params.standardKind } : {}),
    ...(params.standardCode ? { standard_code: params.standardCode } : {}),
    ...(params.assetType ? { asset_type: params.assetType } : {}),
    ...(params.assetId ? { asset_id: params.assetId } : {}),
    ...(params.status ? { status: params.status } : {}),
    page: params.page,
    page_size: params.pageSize,
  };
}

export function buildCoverageParams(
  params: DgCoverageParams,
): Record<string, unknown> {
  return {
    ...(params.kind ? { kind: params.kind } : {}),
    ...(params.domain ? { domain: params.domain } : {}),
    page: params.page,
    page_size: params.pageSize,
  };
}

export function buildSuggestionParams(
  params: DgBindingSuggestionParams,
): Record<string, unknown> {
  return {
    ...(params.standardKind ? { standard_kind: params.standardKind } : {}),
    ...(params.standardCode ? { standard_code: params.standardCode } : {}),
    ...(params.assetType ? { asset_type: params.assetType } : {}),
    ...(params.assetId ? { asset_id: params.assetId } : {}),
    ...(params.limit !== undefined ? { limit: params.limit } : {}),
  };
}

function buildCreateBody(body: DgBindingCreateBody): Record<string, unknown> {
  return {
    standard_kind: body.standardKind,
    standard_code: body.standardCode,
    asset_type: body.assetType,
    asset_id: body.assetId,
    ...(body.note ? { note: body.note } : {}),
    ...(body.source ? { source: body.source } : {}),
    ...(body.confidence !== undefined ? { confidence: body.confidence } : {}),
  };
}

function buildUpdateBody(body: DgBindingUpdateBody): Record<string, unknown> {
  return {
    expected_revision: body.expectedRevision,
    ...(body.note !== undefined ? { note: body.note } : {}),
    ...(body.status !== undefined ? { status: body.status } : {}),
  };
}

// ── 接口 ─────────────────────────────────────────────────────────────

export async function getDomainView(
  params: DgDomainViewParams = {},
): Promise<DgDomainViewResult> {
  const data = await apiClient.get<{
    groups?: unknown; totals?: unknown; empty?: unknown; truncated?: unknown;
  }>(`${BASE}/domains`, { params: buildDomainViewParams(params) });
  const groups = listOrEmpty(data.groups).map((item) =>
    mapDomainGroup(recordOrEmpty(item) as DomainGroupRaw),
  );
  return {
    groups,
    totals: mapDomainTotals(data.totals),
    empty: boolOrFalse(data.empty) || groups.length === 0,
    truncated: boolOrFalse(data.truncated),
  };
}

export async function getResponsibilityView(
  params: DgResponsibilityParams,
): Promise<DgResponsibilityResult> {
  const data = await apiClient.get<{
    rows?: unknown; groups?: unknown; totals?: unknown;
    group_by?: unknown; empty?: unknown; truncated?: unknown;
  }>(`${BASE}/responsibilities`, { params: buildResponsibilityParams(params) });
  // 后端当前返回 `groups`；`rows` 为约定的页面侧名字，两者都兼容。
  const rawRows = Array.isArray(data.rows) ? data.rows : listOrEmpty(data.groups);
  const rows = rawRows.map((item) =>
    mapResponsibilityRow(recordOrEmpty(item) as ResponsibilityRowRaw),
  );
  return {
    rows,
    totals: mapResponsibilityTotals(data.totals),
    groupBy: data.group_by === 'domain' ? 'domain' : 'owner',
    empty: boolOrFalse(data.empty) || rows.length === 0,
    truncated: boolOrFalse(data.truncated),
  };
}

export async function listStandardBindings(
  params: DgBindingListParams,
): Promise<DgBindingListResult> {
  const data = await apiClient.get<{
    items?: unknown; total?: unknown; page?: unknown; page_size?: unknown;
  }>(`${BASE}/standard-bindings`, {
    params: buildBindingListParams(params),
  });
  return {
    items: listOrEmpty(data.items).map((item) =>
      mapBinding(recordOrEmpty(item) as BindingRowRaw),
    ),
    total: numberOr(data.total),
    page: numberOr(data.page, params.page),
    pageSize: numberOr(data.page_size, params.pageSize),
  };
}

export async function createStandardBinding(
  body: DgBindingCreateBody,
): Promise<DgStandardBinding> {
  const data = await apiClient.post<BindingRowRaw>(
    `${BASE}/standard-bindings`, buildCreateBody(body),
  );
  return mapBinding(recordOrEmpty(data) as BindingRowRaw);
}

export async function updateStandardBinding(
  bindingId: string, body: DgBindingUpdateBody,
): Promise<DgStandardBinding> {
  const data = await apiClient.put<BindingRowRaw>(
    `${BASE}/standard-bindings/${encodeURIComponent(bindingId)}`,
    buildUpdateBody(body),
  );
  return mapBinding(recordOrEmpty(data) as BindingRowRaw);
}

export async function deleteStandardBinding(bindingId: string): Promise<void> {
  await apiClient.delete(
    `${BASE}/standard-bindings/${encodeURIComponent(bindingId)}`,
  );
}

export async function getStandardBindingCoverage(
  params: DgCoverageParams,
): Promise<DgCoverageResult> {
  const data = await apiClient.get<{
    items?: unknown; total?: unknown; page?: unknown; page_size?: unknown;
    summary?: unknown; coverage?: unknown;
  }>(`${BASE}/standard-bindings/coverage`, {
    params: buildCoverageParams(params),
  });
  const summaryRaw = isRecord(data.summary)
    ? data.summary
    : (data as unknown as Record<string, unknown>);
  return {
    items: listOrEmpty(data.items).map((item) =>
      mapCoverageRow(recordOrEmpty(item) as CoverageRowRaw),
    ),
    total: numberOr(data.total),
    page: numberOr(data.page, params.page),
    pageSize: numberOr(data.page_size, params.pageSize),
    summary: {
      totalStandards: numberOr(summaryRaw.total_standards),
      boundStandards: numberOr(summaryRaw.bound_standards),
      bindingCount: numberOr(summaryRaw.binding_count),
      // 分母为 0 时后端显式 null；这里保持 null，不伪造成 0。
      coverage: numberOrNull(summaryRaw.coverage),
    },
  };
}

export async function suggestStandardBindings(
  params: DgBindingSuggestionParams = {},
): Promise<DgBindingSuggestion[]> {
  // 网关把候选条件声明为 query（不是 body），POST 只作为「只读计算」语义。
  const data = await apiClient.post<{ items?: unknown }>(
    `${BASE}/standard-bindings/suggest`,
    undefined,
    { params: buildSuggestionParams(params) },
  );
  return listOrEmpty(data.items).map((item) =>
    mapSuggestion(recordOrEmpty(item) as SuggestionRowRaw),
  );
}

export async function getSourceAnchors(
  assetType: string, assetId: string,
): Promise<DgSourceAnchorResult> {
  const data = await apiClient.get<unknown>(`${BASE}/source-anchors`, {
    params: { asset_type: assetType, asset_id: assetId },
  });
  return mapAnchorResult(data);
}
