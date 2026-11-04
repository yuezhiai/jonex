/**
 * 数据合同（ODCS v3.x）的展示与校验语义（W2.3）。
 *
 * 约定：后端 snake_case **只允许出现在 api 层**（`src/api/dataContracts.ts`）；
 * 本文件只接收 camelCase 数据，保留页面侧可直接单测的纯函数。
 */
import type {
  DgContractFormat,
  DgContractScaffoldStats,
  DgDataContractDetail,
  DgDataContractItem,
  DgDataContractStatus,
} from './types';
import { errorMeta } from './utils';

// ── 共用词表 ─────────────────────────────────────────────────────────

/** 合同状态全集（筛选下拉 / 状态 Tag 共用）。 */
export const CONTRACT_STATUSES: DgDataContractStatus[] = [
  'draft',
  'published',
  'retired',
];

/** 合同状态 Tag 配色。 */
export const CONTRACT_STATUS_COLORS: Record<DgDataContractStatus, string> = {
  draft: 'gold',
  published: 'green',
  retired: 'default',
};

/** 合同状态 → i18n 键；未登记的值原样返回（不抛错，避免脏数据吓崩页面）。 */
export function statusLabelKey(status: string): string {
  return CONTRACT_STATUSES.includes(status as DgDataContractStatus)
    ? `dataGovernance.dataContracts.status.${status}`
    : status;
}

/** 合同状态 → Ant Design Tag 颜色；未登记状态回落 default。 */
export function statusColor(status: string): string {
  return (
    CONTRACT_STATUS_COLORS[status as DgDataContractStatus] ?? 'default'
  );
}

/** 详情 → 列表行（去掉 payload），用于写完后就地同步抽屉的 `mode.row`。 */
export function toDataContractItem(
  detail: DgDataContractDetail,
): DgDataContractItem {
  return {
    contractId: detail.contractId,
    dataSourceId: detail.dataSourceId,
    dataSourceName: detail.dataSourceName,
    name: detail.name,
    version: detail.version,
    status: detail.status,
    revision: detail.revision,
    publishedVersion: detail.publishedVersion,
    draftVersion: detail.draftVersion,
    hasDraft: detail.hasDraft,
    updatedAt: detail.updatedAt,
    updatedBy: detail.updatedBy,
  };
}

// ── 乐观锁冲突 ───────────────────────────────────────────────────────

/**
 * 是否为乐观锁冲突：业务码 1003 或 HTTP 409。
 * 统一复用 `utils.errorMeta`，避免各处自行猜测错误对象形状。
 */
export function isConflictError(err: unknown): boolean {
  const meta = errorMeta(err);
  return meta.bizCode === 1003 || meta.status === 409;
}

// ── 内容序列化 / 校验 ────────────────────────────────────────────────

/**
 * payload → 编辑框文本。
 *
 * **只处理 JSON**：YAML 的权威序列化在后端导出接口（`exportDataContract`），
 * 前端不实现 YAML 序列化 —— 页面应对 YAML 新建/编辑禁用该入口。
 * 传入 `yaml` 时返回空串，调用方据此禁用编辑器，避免写出半成品 YAML。
 */
export function formatContractContent(
  payload: Record<string, unknown> | null | undefined,
  format: DgContractFormat,
): string {
  if (format !== 'json') return '';
  return JSON.stringify(payload ?? {}, null, 2);
}

/** JSON 文本校验结果。`error` 是 i18n 键（页面直接 `t(error)`）。 */
export interface JsonContentValidation {
  ok: boolean;
  error?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * 校验编辑框里的 JSON 文本。
 *
 * 只做「能不能提交」的本地前置校验：空文本、语法错误、以及**顶层不是对象**
 * （后端要求合同是对象）都会拦下；语义校验仍由后端负责。
 */
export function validateJsonContent(text: string): JsonContentValidation {
  if (!text || !text.trim()) {
    return {
      ok: false,
      error: 'dataGovernance.dataContracts.contentRequired',
    };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return {
      ok: false,
      error: 'dataGovernance.dataContracts.contentInvalidJson',
    };
  }
  if (!isRecord(parsed)) {
    return {
      ok: false,
      error: 'dataGovernance.dataContracts.contentNotObject',
    };
  }
  return { ok: true };
}

// ── 脚手架摘要 ───────────────────────────────────────────────────────

/** 脚手架统计的展示参数（各计数已归一为非负整数）。 */
export interface ScaffoldSummary extends DgContractScaffoldStats {
  hasSkipped: boolean;
}

function countOr(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 0;
  return Math.trunc(value);
}

/**
 * 脚手架统计 → 展示参数。
 *
 * 页面用 `hasSkipped` 决定是否提示「有表/列被跳过」；计数缺失/非法一律按 0，
 * 不把 `undefined` 漏进文案插值。
 */
export function summarizeScaffold(
  stats: Partial<DgContractScaffoldStats> | null | undefined,
): ScaffoldSummary {
  const tables = countOr(stats?.tables);
  const columns = countOr(stats?.columns);
  const skippedTables = countOr(stats?.skippedTables);
  const skippedColumns = countOr(stats?.skippedColumns);
  return {
    tables,
    columns,
    skippedTables,
    skippedColumns,
    hasSkipped: skippedTables > 0 || skippedColumns > 0,
  };
}
