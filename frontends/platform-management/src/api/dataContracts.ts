/**
 * W2.3 数据合同（ODCS v3.x）取数模块。
 *
 * 网关接口（`apiClient` 的 baseURL 已带 `/api/v1`）：
 *   GET    /data-governance/data-contracts
 *   GET    /data-governance/data-contracts/versions?data_source_id
 *   POST   /data-governance/data-contracts/scaffold-preview
 *   POST   /data-governance/data-contracts/import
 *   GET    /data-governance/data-contracts/{contract_id}
 *   GET    /data-governance/data-contracts/{contract_id}/export?format
 *   POST   /data-governance/data-contracts/drafts
 *   PUT    /data-governance/data-contracts/{contract_id}
 *   POST   /data-governance/data-contracts/{contract_id}/publish
 *   POST   /data-governance/data-contracts/{contract_id}/retire
 *   DELETE /data-governance/data-contracts/{contract_id}
 *   GET    /knowledge-base/data-sources?knowledge_base_id（数据源下拉）
 *
 * 约定：**后端 snake_case 只在本层出现**（行接口 + 映射 + 参数/body 组装都在
 * 本文件），页面与组件只接触 camelCase；`apiClient` 已解包
 * `success_response(data=...)`。
 */
import apiClient from './request';
import { DG_MAX_PAGE_SIZE } from '@/pages/DataGovernance/constant';
import type {
  DgContractDataSourceOption,
  DgContractFormat,
  DgContractScaffoldResult,
  DgContractScaffoldStats,
  DgDataContractDetail,
  DgDataContractDraftBody,
  DgDataContractExportResult,
  DgDataContractImportBody,
  DgDataContractItem,
  DgDataContractListParams,
  DgDataContractListResult,
  DgDataContractStatus,
  DgDataContractUpdateBody,
} from '@/pages/DataGovernance/types';

const BASE = '/data-governance/data-contracts';
const DATA_SOURCES = '/knowledge-base/data-sources';

// ── 后端响应行（snake_case，仅本层可见） ─────────────────────────────

/**
 * 后端数据合同行（snake_case）。可空列用 `?` 兼容裁剪响应。
 *
 * 仅供本模块与单测使用；页面组件不得 import。
 */
export interface DataContractRowRaw {
  contract_id: string;
  data_source_id: string;
  data_source_name?: string | null;
  name: string;
  version?: number | null;
  status: DgDataContractStatus;
  revision?: number | null;
  published_version?: number | null;
  draft_version?: number | null;
  has_draft?: boolean | null;
  updated_at?: string | null;
  updated_by?: string | null;
  /** 仅详情/版本接口返回；形状可信度低，映射时兜底成 `{}`。 */
  payload?: unknown;
}

/** 脚手架预览的后端形状（stats 是 snake_case）。 */
export interface ScaffoldRowRaw {
  data_source_id?: string | null;
  schema_version?: number | null;
  payload?: unknown;
  stats?: {
    tables?: number | null;
    columns?: number | null;
    skipped_tables?: number | null;
    skipped_columns?: number | null;
  } | null;
  warnings?: unknown;
}

/** 数据源列表行的后端形状（只声明下拉用得到的字段）。 */
interface DataSourceRowRaw {
  id?: string | null;
  name?: string | null;
  access_type?: string | null;
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** 非负计数字段：小数取整、负数/非数字按 0 处理。 */
function countOr(value: unknown): number {
  const number = numberOr(value, 0);
  return number > 0 ? Math.trunc(number) : 0;
}

/** 是不是可当作 payload 的对象（null / 数组 / 标量都不算）。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** 版本关系推导「存在未发布草稿」（后端未给出 has_draft 时的兜底）。 */
function deriveHasDraft(
  draftVersion: number | null,
  publishedVersion: number | null,
): boolean {
  if (draftVersion == null) return false;
  return publishedVersion == null || draftVersion > publishedVersion;
}

// ── 纯函数：响应映射 ─────────────────────────────────────────────────

/** 列表行 snake_case → camelCase；可空字段缺失时给安全默认值。 */
export function mapDataContractItem(
  row: DataContractRowRaw,
): DgDataContractItem {
  const publishedVersion = numberOrNull(row.published_version);
  const draftVersion = numberOrNull(row.draft_version);
  return {
    contractId: row.contract_id,
    dataSourceId: row.data_source_id,
    dataSourceName: row.data_source_name ?? null,
    name: row.name,
    version: numberOr(row.version, 0),
    status: row.status,
    revision: numberOr(row.revision, 1),
    publishedVersion,
    draftVersion,
    // 后端显式 false 优先（非管理员时它表达「不展示草稿」）；
    // 字段缺失/为 null 时才用版本关系兜底。
    hasDraft:
      typeof row.has_draft === 'boolean'
        ? row.has_draft
        : deriveHasDraft(draftVersion, publishedVersion),
    updatedAt: row.updated_at ?? null,
    updatedBy: row.updated_by ?? null,
  };
}

/** 详情 = 列表字段 + payload（非对象一律回退空对象）。 */
export function mapDataContractDetail(
  row: DataContractRowRaw,
): DgDataContractDetail {
  return {
    ...mapDataContractItem(row),
    payload: isRecord(row.payload) ? row.payload : {},
  };
}

/** 脚手架预览：stats 的四个计数 snake → camel，warnings 只保留字符串。 */
export function mapScaffoldResult(
  row: ScaffoldRowRaw,
): DgContractScaffoldResult {
  return {
    dataSourceId: row.data_source_id ?? '',
    schemaVersion: numberOrNull(row.schema_version),
    payload: isRecord(row.payload) ? row.payload : {},
    stats: mapScaffoldStats(row.stats),
    warnings: Array.isArray(row.warnings)
      ? row.warnings.filter(
          (item): item is string => typeof item === 'string' && item.trim() !== '',
        )
      : [],
  };
}

/** stats 单独导出便于复用（页面展示摘要时也只接触 camelCase）。 */
export function mapScaffoldStats(
  stats: ScaffoldRowRaw['stats'],
): DgContractScaffoldStats {
  return {
    tables: countOr(stats?.tables),
    columns: countOr(stats?.columns),
    skippedTables: countOr(stats?.skipped_tables),
    skippedColumns: countOr(stats?.skipped_columns),
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
 * - 文本条件去空白，空则不传；
 * - `include_drafts` 只在显式 `true` 时发送；
 * - `page` 夹 ≥1，`page_size` 夹 1..100。
 */
export function buildDataContractListParams(
  params: DgDataContractListParams,
): Record<string, unknown> {
  const query: Record<string, unknown> = {
    page: normalizePage(params.page),
    page_size: clampPageSize(params.pageSize),
  };

  const textFields: Array<[string, string | undefined]> = [
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
export function buildDataContractDraftBody(
  body: DgDataContractDraftBody,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    data_source_id: body.dataSourceId,
  };
  if (body.payload !== undefined) payload.payload = body.payload;
  if (body.name !== undefined) payload.name = body.name;
  if (body.baseVersion !== undefined) payload.base_version = body.baseVersion;
  return payload;
}

/** 更新草稿 body：`expectedRevision` → `expected_revision`，name/payload 按需发送。 */
export function buildDataContractUpdateBody(
  body: DgDataContractUpdateBody,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    expected_revision: body.expectedRevision,
  };
  if (body.name !== undefined) payload.name = body.name;
  if (body.payload !== undefined) payload.payload = body.payload;
  return payload;
}

/** 导入 body：format 为空则不传（后端按内容首字符判定），yml 归一为 yaml。 */
export function buildDataContractImportBody(
  body: DgDataContractImportBody,
): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    data_source_id: body.dataSourceId,
    content: body.content,
  };
  const format = normalizeFormat(body.format);
  if (format) payload.format = format;
  return payload;
}

/** 导出格式归一：空值缺省 json，`yml` 视作 yaml；未知值原样交给后端报错。 */
export function normalizeFormat(value: unknown): DgContractFormat | '' {
  if (typeof value !== 'string') return '';
  const normalized = value.trim().toLowerCase();
  if (!normalized) return '';
  return normalized === 'yml' ? 'yaml' : (normalized as DgContractFormat);
}

// ── 接口封装 ─────────────────────────────────────────────────────────

function contractPath(contractId: string): string {
  return `${BASE}/${encodeURIComponent(contractId)}`;
}

/** 列表；响应为 `{items, total, page, page_size}`，这里只消费前两者。 */
export async function listDataContracts(
  params: DgDataContractListParams,
): Promise<DgDataContractListResult> {
  const data = await apiClient.get<{
    items?: DataContractRowRaw[];
    total?: number;
  }>(BASE, { params: buildDataContractListParams(params) });
  return {
    items: (data.items ?? []).map(mapDataContractItem),
    total: data.total ?? 0,
  };
}

export async function getDataContract(
  contractId: string,
): Promise<DgDataContractDetail> {
  const data = await apiClient.get<DataContractRowRaw>(
    contractPath(contractId),
  );
  return mapDataContractDetail(data);
}

/** 同一数据源的版本历史（倒序由后端保证）。 */
export async function listDataContractVersions(
  dataSourceId: string,
): Promise<DgDataContractDetail[]> {
  const data = await apiClient.get<{ items?: DataContractRowRaw[] }>(
    `${BASE}/versions`,
    { params: { data_source_id: dataSourceId } },
  );
  return (data.items ?? []).map(mapDataContractDetail);
}

/** 由该数据源已编译 schema 生成脚手架（只读，不落库）。 */
export async function previewDataContractScaffold(
  dataSourceId: string,
): Promise<DgContractScaffoldResult> {
  const data = await apiClient.post<ScaffoldRowRaw>(
    `${BASE}/scaffold-preview`,
    { data_source_id: dataSourceId },
  );
  return mapScaffoldResult(data);
}

export async function createDataContractDraft(
  body: DgDataContractDraftBody,
): Promise<DgDataContractDetail> {
  const data = await apiClient.post<DataContractRowRaw>(
    `${BASE}/drafts`,
    buildDataContractDraftBody(body),
  );
  return mapDataContractDetail(data);
}

export async function updateDataContractDraft(
  contractId: string,
  body: DgDataContractUpdateBody,
): Promise<DgDataContractDetail> {
  const data = await apiClient.put<DataContractRowRaw>(
    contractPath(contractId),
    buildDataContractUpdateBody(body),
  );
  return mapDataContractDetail(data);
}

export async function publishDataContract(
  contractId: string,
  expectedRevision: number,
): Promise<DgDataContractDetail> {
  const data = await apiClient.post<DataContractRowRaw>(
    `${contractPath(contractId)}/publish`,
    { expected_revision: expectedRevision },
  );
  return mapDataContractDetail(data);
}

export async function retireDataContract(
  contractId: string,
): Promise<DgDataContractDetail> {
  const data = await apiClient.post<DataContractRowRaw>(
    `${contractPath(contractId)}/retire`,
  );
  return mapDataContractDetail(data);
}

/** 删除草稿（仅 `status=draft` 的行可删；其余后端回冲突）。 */
export async function deleteDataContractDraft(
  contractId: string,
): Promise<void> {
  await apiClient.delete(contractPath(contractId));
}

/** 导入 JSON/YAML 合同：解析与校验都在后端，前端原样提交内容。 */
export async function importDataContract(
  body: DgDataContractImportBody,
): Promise<DgDataContractDetail> {
  const data = await apiClient.post<DataContractRowRaw>(
    `${BASE}/import`,
    buildDataContractImportBody(body),
  );
  return mapDataContractDetail(data);
}

/**
 * 导出合同。`format` 缺省 json；返回内容与文件名，页面自行触发下载
 * （本层不做 Blob 转换）。
 */
export async function exportDataContract(
  contractId: string,
  format?: DgContractFormat,
): Promise<DgDataContractExportResult> {
  const normalized = normalizeFormat(format) || 'json';
  const data = await apiClient.get<{
    content?: string;
    format?: string;
    filename?: string;
  }>(`${contractPath(contractId)}/export`, {
    params: { format: normalized },
  });
  return {
    content: typeof data.content === 'string' ? data.content : '',
    format: data.format === 'yaml' ? 'yaml' : 'json',
    filename: typeof data.filename === 'string' ? data.filename : '',
  };
}

/**
 * 可建合同的数据源下拉：只保留 `access_type === 'database'` 的源
 * （合同是数据库表的 ODCS 描述，文件/API/存储源没有库表结构）。
 */
export async function listContractDataSources(
  kbId: string,
): Promise<DgContractDataSourceOption[]> {
  const data = await apiClient.get<{ items?: DataSourceRowRaw[] }>(
    DATA_SOURCES,
    { params: { knowledge_base_id: kbId } },
  );
  return (data.items ?? [])
    .filter((item) => item.access_type === 'database')
    .map((item) => ({
      id: item.id ?? '',
      name: item.name ?? item.id ?? '',
      accessType: item.access_type ?? '',
    }))
    .filter((item) => item.id !== '');
}
