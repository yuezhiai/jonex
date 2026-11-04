/**
 * 数据治理资产目录的取数模块（第 2 期前端）。
 *
 * 后端 4 个接口：
 *   GET /data-governance/assets
 *   GET /data-governance/assets/{asset_type}/{asset_id}
 *   PUT /data-governance/assets/{asset_type}/{asset_id}/metadata
 *   GET /data-governance/assets/{asset_type}/{asset_id}/lineage
 *
 * 约定：**所有后端 snake_case 只在本层转换**，页面与组件只接触 camelCase。
 * 映射函数是纯函数（直接可测），async 函数只负责拼路径/参数并调用 `apiClient`。
 */
import apiClient from './request';
import { DG_MAX_PAGE_SIZE } from '@/pages/DataGovernance/constant';
import type {
  DgAssetDetail,
  DgAssetItem,
  DgAssetListParams,
  DgAssetListResult,
  DgAssetType,
  DgClassificationConfig,
  DgClassificationDefault,
  DgClassificationDefaultPatch,
  DgClassificationSource,
  DgDirection,
  DgLineage,
  DgMetadataPatch,
  DgLifecycleResult,
  DgQuality,
  DgQualityVerdict,
  DgRetentionPolicy,
  DgRetentionPolicyPatch,
  DgRetentionTier,
  DgRetentionWriteScope,
  DgSensitivity,
  DgUserClearance,
} from '@/pages/DataGovernance/types';

// ── 后端响应行（snake_case，仅本层可见） ──────────────────────────────

interface DgAssetItemRow {
  asset_type: DgAssetType;
  asset_id: string;
  name: string;
  kb_id: string;
  kb_name: string;
  space_id: string;
  status: string;
  updated_at: string;
  owner: string | null;
  domain: string | null;
  sensitivity: DgSensitivity | null;
  /** 列表必有；详情接口把档位收进 `quality`，顶层不再出现。 */
  quality_verdict?: DgQualityVerdict;
  /** 分类类目编码；旧后端/半截响应可能缺失。 */
  classification_categories?: string[];
  classification_sources?: {
    sensitivity?: DgClassificationSource;
    classification_categories?: DgClassificationSource;
  };
  // ── W2.1 派生资产（表 / 列 / 指标）列表字段 ──────────────────────
  /** 上级标识：列 → `ds:table`，表 → 数据源 id，指标 → KB id。 */
  parent_id?: string | null;
  /** 表/列所属数据源；指标为所在 KB；文档/数据源本身为 null。 */
  source_id?: string | null;
  /** 来源展示名（列表 SQL 计算列）：表/列为数据源名，指标为 KB 名。 */
  source_label?: string | null;
  /** per-source compiled schema / 抽象 schema 的版本号。 */
  schema_version?: number | null;
}

interface DgAssetDetailRow extends DgAssetItemRow {
  file_size: number | null;
  mime_type: string | null;
  data_source_type: string | null;
  folder_id: string | null;
  content_generation: number | null;
  ontology_status: string | null;
  access_type: string | null;
  sync_mode: string | null;
  last_sync_at: string | null;
  last_sync_status: string | null;
  document_count: number | null;
  quality?: DgQuality | null;
  /** 详情接口附加：创建时间（列表不返回）。 */
  created_at?: string | null;
  /** 详情接口附加：未归档为 null（列表不返回）。 */
  archived_at?: string | null;
  /** 详情接口附加：生效保留策略投影（投影失败为 null）。 */
  retention_policy?: DgRetentionPolicyRow | null;
  // ── W2.1 派生资产（表 / 列 / 指标）详情字段 ──────────────────────
  data_source_id?: string | null;
  /** 物理表名（列资产为其所属表）。 */
  table?: string | null;
  column_type?: string | null;
  primary_key?: boolean | null;
  display_name?: string | null;
  /** 表的列清单（snake_case，映射时逐项转 camelCase）。 */
  columns?: DgColumnRow[] | null;
  column_count?: number | null;
  description?: string | null;
  additivity?: string | null;
  aliases?: string[] | null;
  /** 指标落点（snake_case，映射时逐项转 camelCase）。 */
  landings?: DgLandingRow[] | null;
  unresolved?: boolean | null;
  sync_status?: string | null;
}

/** 表详情里的列行（来自 active per-source compiled schema）。 */
interface DgColumnRow {
  name: string;
  type: string | null;
  primary_key?: boolean | null;
  display_name?: string | null;
}

/** 指标落点行（`table` 为空 = 未解析到物理表）。 */
interface DgLandingRow {
  data_source_id: string;
  entity_name?: string;
  di_metric?: string;
  table?: string | null;
}

/**
 * 生效保留策略行（snake_case；`sources` 的键同样是 snake_case）。
 * 子键全部可选：运行时缺失由映射层兜底，避免旧后端/半截响应让页面崩。
 */
interface DgRetentionPolicyRow {
  scope?: DgRetentionTier;
  effective_from?: DgRetentionTier;
  retention_days?: number;
  grace_days?: number;
  sources?: {
    retention_days?: DgRetentionTier;
    grace_days?: DgRetentionTier;
  };
}

interface DgGraphNodeRow {
  id: string;
  type: string;
  label: string;
  meta?: Record<string, unknown>;
}

interface DgGraphEdgeRow {
  source: string;
  target: string;
  type: string;
  meta?: Record<string, unknown>;
}

interface DgLineageRow {
  asset: { asset_type: DgAssetType; asset_id: string; name: string };
  nodes: DgGraphNodeRow[];
  edges: DgGraphEdgeRow[];
  // counts / truncated 允许整体缺失：映射层与 nodes/edges 统一按「缺省给默认值」兜底。
  // 派生资产（表/列/指标）的计数是 `{nodes, edges}`，文档图才是四类计数。
  counts?: {
    chunks?: number | null;
    entities?: number;
    relations?: number;
    citations?: number;
    nodes?: number;
    edges?: number;
  };
  // 文档图是对象；派生资产血缘直接给 `false`，两种形状都要兼容。
  truncated?:
    | {
        entities?: boolean;
        relations?: boolean;
        chunks?: boolean;
      }
    | boolean;
  /** 派生资产血缘的降级标记（文档血缘缺省 false）。 */
  degraded?: boolean;
  /** 指标血缘无落点（其他资产缺省 false）。 */
  unresolved?: boolean;
}

// ── 纯函数：参数组装 ─────────────────────────────────────────────────

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
 * camelCase 查询参数 → 后端 snake_case。
 *
 * - `asset_type` 缺省 `all`；
 * - 文本类字段去空白，空则不传（后端按「有值才加条件」处理）；
 * - `sensitivity` **原样透传**（含读侧专用的 `unclassified`）；
 * - `page` 取整并夹到 ≥1，`page_size` 夹 1..100（两者口径对称）。
 */
export function buildListParams(params: DgAssetListParams): Record<string, unknown> {
  const query: Record<string, unknown> = {
    asset_type: params.assetType ?? 'all',
    page: normalizePage(params.page),
    page_size: clampPageSize(params.pageSize),
  };

  const textFields: Array<[string, string | undefined]> = [
    ['domain_space_id', params.spaceId],
    ['kb_id', params.kbId],
    ['domain', params.domain],
    ['owner', params.owner],
    ['category', params.category],
    ['status', params.status],
    ['keyword', params.keyword],
  ];
  for (const [key, value] of textFields) {
    const trimmed = value?.trim();
    if (trimmed) query[key] = trimmed;
  }

  if (params.sensitivity !== undefined) {
    query.sensitivity = params.sensitivity;
  }
  return query;
}

// ── 纯函数：响应映射 ─────────────────────────────────────────────────

/**
 * 列表项映射。
 *
 * `quality_verdict=null` 必须保留为 `null` —— 它表示「质量未知/未投影」，
 * 不能伪造成 `ok`；`sensitivity=null` 同理（未分级 ≠ public）。
 */
export function mapAssetItem(row: DgAssetItemRow): DgAssetItem {
  return {
    assetType: row.asset_type,
    assetId: row.asset_id,
    name: row.name,
    kbId: row.kb_id,
    kbName: row.kb_name,
    spaceId: row.space_id,
    status: row.status,
    updatedAt: row.updated_at,
    owner: row.owner ?? null,
    domain: row.domain ?? null,
    sensitivity: row.sensitivity ?? null,
    qualityVerdict: row.quality_verdict ?? null,
    classificationCategories: row.classification_categories ?? [],
    classificationSources: {
      sensitivity: row.classification_sources?.sensitivity ?? 'none',
      classificationCategories:
        row.classification_sources?.classification_categories ?? 'none',
    },
    // 派生资产字段只在后端给出键时出现（旧响应/文档行不受影响）。
    ...(row.parent_id !== undefined ? { parentId: row.parent_id } : {}),
    ...(row.source_id !== undefined ? { sourceId: row.source_id } : {}),
    ...(row.source_label !== undefined ? { sourceLabel: row.source_label } : {}),
    ...(row.schema_version !== undefined ? { schemaVersion: row.schema_version } : {}),
  };
}

/**
 * 保留策略行（必有）→ camelCase 生效策略。
 *
 * 防御口径与 `mapLineage` 一致：`sources` 或子键缺失时不崩，按
 * `effective_from → scope → system` 回落；天数缺失按 0（后端 0 = 不自动到期）。
 */
function mapRetentionPolicyRow(row: DgRetentionPolicyRow): DgRetentionPolicy {
  const scope = row.scope ?? 'system';
  return {
    scope,
    effectiveFrom: row.effective_from ?? scope,
    retentionDays: row.retention_days ?? 0,
    graceDays: row.grace_days ?? 0,
    sources: {
      retentionDays:
        row.sources?.retention_days ?? row.effective_from ?? scope,
      graceDays: row.sources?.grace_days ?? scope,
    },
  };
}

/** 策略投影可能整体缺失（数据源/投影失败）→ null，不能伪造系统默认值。 */
export function mapRetentionPolicy(
  row: DgRetentionPolicyRow | null | undefined,
): DgRetentionPolicy | null {
  return row ? mapRetentionPolicyRow(row) : null;
}

/** 详情映射：保留 `quality.reasons` 与文档/数据源两套专属字段。 */
export function mapAssetDetail(row: DgAssetDetailRow): DgAssetDetail {
  const quality = row.quality ?? { verdict: null as DgQualityVerdict, reasons: [] };
  return {
    // 档位以**嵌套 quality.verdict 优先**：后端详情把档位收进 quality，顶层
    // quality_verdict 通常缺失；两处不一致时以详情声明为准，顶层仅作回退。
    ...mapAssetItem({
      ...row,
      quality_verdict: row.quality?.verdict ?? row.quality_verdict ?? null,
    }),
    fileSize: row.file_size ?? null,
    mimeType: row.mime_type ?? null,
    dataSourceType: row.data_source_type ?? null,
    folderId: row.folder_id ?? null,
    contentGeneration: row.content_generation ?? null,
    ontologyStatus: row.ontology_status ?? null,
    accessType: row.access_type ?? null,
    syncMode: row.sync_mode ?? null,
    lastSyncAt: row.last_sync_at ?? null,
    lastSyncStatus: row.last_sync_status ?? null,
    documentCount: row.document_count ?? null,
    quality: {
      verdict: quality.verdict ?? null,
      reasons: quality.reasons ?? [],
    },
    createdAt: row.created_at ?? null,
    archivedAt: row.archived_at ?? null,
    retentionPolicy: mapRetentionPolicy(row.retention_policy),
    // ── W2.1 派生资产专属字段 ────────────────────────────────────
    // 与旧响应/文档详情保持兼容：只有后端给出键时才写入，避免把
    // 「不适用」伪造成 null 而污染既有页面断言与展示。
    ...(row.data_source_id !== undefined ? { dataSourceId: row.data_source_id } : {}),
    ...(row.table !== undefined ? { table: row.table } : {}),
    ...(row.column_type !== undefined ? { columnType: row.column_type } : {}),
    ...(row.primary_key !== undefined ? { primaryKey: row.primary_key } : {}),
    ...(row.display_name !== undefined ? { displayName: row.display_name } : {}),
    // 列清单的嵌套键必须逐项转换，不能整体透传 snake_case。
    ...(row.columns !== undefined
      ? {
          columns: (row.columns ?? []).map((column) => ({
            name: column.name,
            type: column.type ?? null,
            primaryKey: Boolean(column.primary_key),
            displayName: column.display_name ?? null,
          })),
        }
      : {}),
    ...(row.column_count !== undefined ? { columnCount: row.column_count } : {}),
    ...(row.description !== undefined ? { description: row.description } : {}),
    ...(row.additivity !== undefined ? { additivity: row.additivity } : {}),
    ...(row.aliases !== undefined ? { aliases: row.aliases ?? [] } : {}),
    ...(row.landings !== undefined
      ? {
          landings: (row.landings ?? []).map((landing) => ({
            dataSourceId: landing.data_source_id,
            entityName: landing.entity_name,
            diMetric: landing.di_metric,
            table: landing.table ?? null,
          })),
        }
      : {}),
    ...(row.unresolved !== undefined ? { unresolved: Boolean(row.unresolved) } : {}),
    ...(row.sync_status !== undefined ? { syncStatus: row.sync_status } : {}),
  };
}

/**
 * 血缘图映射。
 *
 * - 文档图：`counts.chunks` 仅在 depth=2 时有值，其余为 null；`truncated` 是
 *   三个布尔构成的对象。
 * - 派生资产（表/列/指标）：`counts` 为 `{nodes, edges}`（按键透传），
 *   `truncated` 直接是布尔值；`true` 表示整图被截断。
 */
export function mapLineage(row: DgLineageRow): DgLineage {
  // 派生资产把 `truncated` 直接给布尔值；对象形状则逐键读取。
  const truncatedAll = row.truncated === true;
  const truncated =
    typeof row.truncated === 'object' && row.truncated !== null ? row.truncated : {};
  return {
    asset: {
      assetType: row.asset.asset_type,
      assetId: row.asset.asset_id,
      name: row.asset.name,
    },
    nodes: (row.nodes ?? []).map((node) => ({
      id: node.id,
      type: node.type,
      label: node.label,
      meta: node.meta ?? {},
    })),
    edges: (row.edges ?? []).map((edge) => ({
      source: edge.source,
      target: edge.target,
      type: edge.type,
      meta: edge.meta ?? {},
    })),
    // 防御口径与 nodes/edges 一致：可选链 + 默认值，单段缺失不拖垮整图。
    counts: {
      chunks: row.counts?.chunks ?? null,
      entities: row.counts?.entities ?? 0,
      relations: row.counts?.relations ?? 0,
      citations: row.counts?.citations ?? 0,
      // 派生资产按 `{nodes, edges}` 给计数：仅在键存在时透传，
      // 文档图保持原四键输出，不多出 nodes/edges。
      ...(row.counts?.nodes !== undefined ? { nodes: row.counts.nodes } : {}),
      ...(row.counts?.edges !== undefined ? { edges: row.counts.edges } : {}),
    },
    truncated: {
      entities: truncatedAll || Boolean(truncated.entities),
      relations: truncatedAll || Boolean(truncated.relations),
      chunks: truncatedAll || Boolean(truncated.chunks),
    },
    // 仅派生资产响应会带这两个键；文档血缘保持原输出形状。
    ...(row.degraded !== undefined ? { degraded: Boolean(row.degraded) } : {}),
    ...(row.unresolved !== undefined ? { unresolved: Boolean(row.unresolved) } : {}),
  };
}

// ── 接口封装 ─────────────────────────────────────────────────────────

const ASSETS_PATH = '/data-governance/assets';

function assetPath(assetType: DgAssetType, assetId: string): string {
  return `${ASSETS_PATH}/${encodeURIComponent(assetType)}/${encodeURIComponent(assetId)}`;
}

export async function listAssets(params: DgAssetListParams): Promise<DgAssetListResult> {
  const data = await apiClient.get<{ items: DgAssetItemRow[]; total: number }>(
    ASSETS_PATH,
    { params: buildListParams(params) },
  );
  return {
    items: (data.items ?? []).map(mapAssetItem),
    total: data.total ?? 0,
  };
}

export async function getAsset(
  assetType: DgAssetType,
  assetId: string,
): Promise<DgAssetDetail> {
  const data = await apiClient.get<DgAssetDetailRow>(assetPath(assetType, assetId));
  return mapAssetDetail(data);
}

/**
 * 更新资产元数据。
 *
 * body **就是调用方给的 patch**：`asset_type` / `asset_id` 走路径，租户由
 * 认证上下文透传 —— 三者都不能塞进 body（后端 DTO `extra="forbid"`）。
 */
export async function updateAssetMetadata(
  assetType: DgAssetType,
  assetId: string,
  patch: DgMetadataPatch,
): Promise<DgAssetDetail> {
  const data = await apiClient.put<DgAssetDetailRow>(
    `${assetPath(assetType, assetId)}/metadata`,
    patch,
  );
  return mapAssetDetail(data);
}

export async function getAssetLineage(
  assetType: DgAssetType,
  assetId: string,
  direction: DgDirection = 'both',
  depth: 1 | 2 = 1,
): Promise<DgLineage> {
  const data = await apiClient.get<DgLineageRow>(`${assetPath(assetType, assetId)}/lineage`, {
    params: { direction, depth },
  });
  return mapLineage(data);
}

// ── 第 3 期生命周期：归档 / 恢复 / 销毁 ──────────────────────────────
// 都是 KB 能力 action，网关端点统一以 `/knowledge-base/documents/{id}` 开头，
// `knowledge_base_id` 走查询参数（后端是 `Query(...)`，不是 body）。

type DgDocumentLifecycleAction = 'archive' | 'restore' | 'destroy';

/** 后端生命周期响应行：键本身已是 camelCase，仅做可选字段收敛。 */
interface DgLifecycleResultRow {
  id: string;
  status?: string;
  archived?: boolean;
  restored?: boolean;
  deleted?: boolean;
}

/** 只保留后端实际给出的键，避免 `{status: undefined}` 混进页面状态。 */
function mapLifecycleResult(row: DgLifecycleResultRow): DgLifecycleResult {
  return {
    id: row.id,
    ...(row.status !== undefined ? { status: row.status } : {}),
    ...(row.archived !== undefined ? { archived: row.archived } : {}),
    ...(row.restored !== undefined ? { restored: row.restored } : {}),
    ...(row.deleted !== undefined ? { deleted: row.deleted } : {}),
  };
}

async function postDocumentLifecycle(
  assetType: DgAssetType,
  assetId: string,
  knowledgeBaseId: string,
  action: DgDocumentLifecycleAction,
): Promise<DgLifecycleResult> {
  if (assetType !== 'document') {
    // 数据源没有归档/销毁语义；静默打到 documents 路径只会得到后端 404。
    throw new Error(`生命周期操作仅支持文档资产，收到：${assetType}`);
  }
  const data = await apiClient.post<DgLifecycleResultRow>(
    `/knowledge-base/documents/${encodeURIComponent(assetId)}/${action}`,
    undefined,
    { params: { knowledge_base_id: knowledgeBaseId } },
  );
  return mapLifecycleResult(data);
}

export async function archiveAsset(
  assetType: DgAssetType,
  assetId: string,
  knowledgeBaseId: string,
): Promise<DgLifecycleResult> {
  return postDocumentLifecycle(assetType, assetId, knowledgeBaseId, 'archive');
}

export async function restoreAsset(
  assetType: DgAssetType,
  assetId: string,
  knowledgeBaseId: string,
): Promise<DgLifecycleResult> {
  return postDocumentLifecycle(assetType, assetId, knowledgeBaseId, 'restore');
}

export async function destroyAsset(
  assetType: DgAssetType,
  assetId: string,
  knowledgeBaseId: string,
): Promise<DgLifecycleResult> {
  return postDocumentLifecycle(assetType, assetId, knowledgeBaseId, 'destroy');
}

// ── 第 3 期保留策略：查询 / 写入 ─────────────────────────────────────

const RETENTION_POLICY_PATH = '/data-governance/retention-policy';

export async function getRetentionPolicy(
  scopeType: DgRetentionWriteScope,
  scopeId?: string,
): Promise<DgRetentionPolicy> {
  const params: Record<string, string> = { scope_type: scopeType };
  if (scopeId) params.scope_id = scopeId;
  const data = await apiClient.get<DgRetentionPolicyRow>(RETENTION_POLICY_PATH, { params });
  return mapRetentionPolicyRow(data);
}

/**
 * 写入某级策略覆盖：**只发调用方给了的键**（显式 `null` = 清除该级覆盖，
 * 未给的键保持继承）；返回更新后的生效策略。
 *
 * `system` 不可写（编译期拒绝）；kb/document 的 `scopeId` 必填。
 */
export function setRetentionPolicy(
  scopeType: 'tenant',
  scopeId: string | null | undefined,
  patch: DgRetentionPolicyPatch,
): Promise<DgRetentionPolicy>;
export function setRetentionPolicy(
  scopeType: 'document' | 'kb',
  scopeId: string,
  patch: DgRetentionPolicyPatch,
): Promise<DgRetentionPolicy>;
export async function setRetentionPolicy(
  scopeType: DgRetentionWriteScope,
  scopeId: string | null | undefined,
  patch: DgRetentionPolicyPatch,
): Promise<DgRetentionPolicy> {
  if (!('retention_days' in patch) && !('grace_days' in patch)) {
    // 后端对空补丁直接 400；前端提前拒绝，省一次必然失败的往返。
    throw new Error('没有可更新的保留策略字段');
  }
  const body: Record<string, unknown> = { scope_type: scopeType };
  if (scopeId) body.scope_id = scopeId;
  if ('retention_days' in patch) body.retention_days = patch.retention_days;
  if ('grace_days' in patch) body.grace_days = patch.grace_days;
  const data = await apiClient.put<DgRetentionPolicyRow>(RETENTION_POLICY_PATH, body);
  return mapRetentionPolicyRow(data);
}

// ── 第 4 期分级分类：配置 / 默认值 / 批量更新 ────────────────────────
// 所有 snake_case 只在本层转换；配置项 name 是管理员自维护文案，
// 原样透传（不翻译），级别编码的展示文案由 utils.levelLabel 处理。

interface DgClassificationConfigRow {
  levels?: Array<{ code: string; name: string; rank: number; is_active: boolean }>;
  categories?: Array<{ code: string; name: string; is_active: boolean }>;
}

interface DgClassificationDefaultRow {
  scope_type?: 'tenant' | 'kb';
  scope_id?: string;
  level_code?: string | null;
  categories?: string[] | null;
}

function mapClassificationConfigRow(data: DgClassificationConfigRow): DgClassificationConfig {
  return {
    levels: (data.levels ?? []).map((row) => ({
      code: row.code,
      name: row.name,
      rank: row.rank,
      isActive: Boolean(row.is_active),
    })),
    categories: (data.categories ?? []).map((row) => ({
      code: row.code,
      name: row.name,
      isActive: Boolean(row.is_active),
    })),
  };
}

/** 默认值映射：响应缺 `scope_type` / `scope_id` 时按调用方入参回落。 */
function mapClassificationDefaultRow(
  data: DgClassificationDefaultRow,
  fallbackScope: 'tenant' | 'kb',
  fallbackId?: string,
): DgClassificationDefault {
  return {
    scopeType: data.scope_type ?? fallbackScope,
    scopeId: data.scope_id ?? fallbackId ?? '',
    levelCode: data.level_code ?? null,
    categories: data.categories ?? null,
  };
}

const CLASSIFICATION_CONFIG_PATH = '/data-governance/classification-config';
const CLASSIFICATION_DEFAULT_PATH = '/data-governance/classification-default';

export async function listClassificationConfig(): Promise<DgClassificationConfig> {
  const data = await apiClient.get<DgClassificationConfigRow>(CLASSIFICATION_CONFIG_PATH);
  return mapClassificationConfigRow(data);
}

export async function upsertClassificationLevel(
  code: string,
  patch: { name?: string; rank?: number; is_active?: boolean },
): Promise<void> {
  await apiClient.put<void>(
    `/data-governance/classification-levels/${encodeURIComponent(code)}`,
    patch,
  );
}

export async function upsertClassificationCategory(
  code: string,
  patch: { name?: string; is_active?: boolean },
): Promise<void> {
  await apiClient.put<void>(
    `/data-governance/classification-categories/${encodeURIComponent(code)}`,
    patch,
  );
}

/**
 * 作用域 id 归一：trim 后为空视为**未提供**。
 * KB 级必须有 id（缺了请求毫无意义，且后端会按空串查库），直接抛错；
 * 租户级忽略。
 */
function normalizeScopeId(
  scopeType: 'tenant' | 'kb',
  scopeId: string | null | undefined,
): string | null {
  const trimmed = scopeId?.trim();
  if (trimmed) return trimmed;
  if (scopeType === 'kb') throw new Error('知识库级分级默认值需要 scope_id');
  return null;
}

export function getClassificationDefault(
  scopeType: 'tenant',
  scopeId?: string | null,
): Promise<DgClassificationDefault>;
export function getClassificationDefault(
  scopeType: 'kb',
  scopeId: string,
): Promise<DgClassificationDefault>;
export async function getClassificationDefault(
  scopeType: 'tenant' | 'kb',
  scopeId?: string | null,
): Promise<DgClassificationDefault> {
  const id = normalizeScopeId(scopeType, scopeId);
  const params: Record<string, string> = { scope_type: scopeType };
  if (id) params.scope_id = id;
  const data = await apiClient.get<DgClassificationDefaultRow>(CLASSIFICATION_DEFAULT_PATH, {
    params,
  });
  return mapClassificationDefaultRow(data, scopeType, id ?? undefined);
}

/**
 * 写入租户/KB 默认分级：只发 patch 里出现的键（显式 `null` = 清除该级默认），
 * 未出现的键保持原值。
 */
export function setClassificationDefault(
  scopeType: 'tenant',
  scopeId: string | null | undefined,
  patch: DgClassificationDefaultPatch,
): Promise<DgClassificationDefault>;
export function setClassificationDefault(
  scopeType: 'kb',
  scopeId: string,
  patch: DgClassificationDefaultPatch,
): Promise<DgClassificationDefault>;
export async function setClassificationDefault(
  scopeType: 'tenant' | 'kb',
  scopeId: string | null | undefined,
  patch: DgClassificationDefaultPatch,
): Promise<DgClassificationDefault> {
  const id = normalizeScopeId(scopeType, scopeId);
  const body: Record<string, unknown> = { scope_type: scopeType };
  if (id) body.scope_id = id;
  if ('level_code' in patch) body.level_code = patch.level_code;
  if ('categories' in patch) body.categories = patch.categories;
  const data = await apiClient.put<DgClassificationDefaultRow>(
    CLASSIFICATION_DEFAULT_PATH,
    body,
  );
  return mapClassificationDefaultRow(data, scopeType, id ?? undefined);
}

export async function batchUpdateClassification(payload: {
  assets: Array<{ asset_type: DgAssetType; asset_id: string }>;
  sensitivity?: string | null;
  categories?: string[] | null;
}): Promise<{ updated: number }> {
  return apiClient.post<{ updated: number }>(
    '/data-governance/assets/classification/batch',
    payload,
  );
}

// ── 用户密级（第 4 期，规格 §6.3） ──────────────────────────────────

const USER_CLEARANCE_PATH = '/data-governance/classification-users';

/**
 * 用户密级行映射。`display_name` 为空时回落到 `username`，
 * 列表「用户」列不会出现空白；`max_level_code=null` 原样保留。
 */
export function mapUserClearance(row: {
  user_id: string;
  username: string;
  display_name: string;
  max_level_code: string | null;
  is_active: boolean;
}): DgUserClearance {
  return {
    userId: row.user_id,
    username: row.username,
    displayName: row.display_name || row.username,
    maxLevelCode: row.max_level_code,
    isActive: row.is_active,
  };
}

export async function listUserClearances(): Promise<DgUserClearance[]> {
  const data = await apiClient.get<{
    items?: Array<Parameters<typeof mapUserClearance>[0]>;
  }>(USER_CLEARANCE_PATH);
  return (data.items ?? []).map(mapUserClearance);
}

export async function setUserClearance(
  userId: string,
  maxLevelCode: string | null,
): Promise<void> {
  await apiClient.put<void>(
    `${USER_CLEARANCE_PATH}/${encodeURIComponent(userId)}`,
    { max_level_code: maxLevelCode },
  );
}
