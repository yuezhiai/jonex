/**
 * 标准资产（术语 / 数据元 / 指标口径卡）的展示与校验语义（W2.2）。
 *
 * 约定：后端 snake_case **只允许出现在 api 层**
 * （`src/api/standardAssets.ts`，行接口/映射/参数组装都在那里）；本文件只接收
 * camelCase 数据，保留页面侧可直接单测的纯函数。
 */
import type {
  DgStandardAssetDetail,
  DgStandardAssetItem,
  DgStandardAssetKind,
  DgStandardAssetStatus,
} from './types';
import { errorMeta } from './utils';

// ── 共用词表 ─────────────────────────────────────────────────────────

/** 三类标准资产（分段控件 / 新建表单共用）。 */
export const KINDS: DgStandardAssetKind[] = ['term', 'data_element', 'metric_card'];

/** 标准资产状态 Tag 配色（列表与抽屉共用）。 */
export const STATUS_COLORS: Record<DgStandardAssetStatus, string> = {
  draft: 'gold',
  published: 'green',
  retired: 'default',
};

// ── 乐观锁冲突 ───────────────────────────────────────────────────────

/**
 * 是否为乐观锁冲突：业务码 1003 或 HTTP 409。
 * 统一复用 `utils.errorMeta`，避免各处自行猜测错误对象形状。
 */
export function isConflictError(err: unknown): boolean {
  const meta = errorMeta(err);
  return meta.bizCode === 1003 || meta.status === 409;
}

// ── i18n ─────────────────────────────────────────────────────────────

const LABEL_KEYS: Record<string, string> = {
  term: 'dataGovernance.standardAssets.kind.term',
  data_element: 'dataGovernance.standardAssets.kind.data_element',
  metric_card: 'dataGovernance.standardAssets.kind.metric_card',
};

/** kind → i18n 键。未登记的 kind 原样返回（不抛错，避免脏数据吓崩页面）。 */
export function kindLabelKey(kind: string): string {
  return LABEL_KEYS[kind] ?? kind;
}

// ── 草稿判定 ─────────────────────────────────────────────────────────

/**
 * 是否存在「未发布的草稿」。
 *
 * - 没有草稿版本 → false；
 * - 从未发布过（publishedVersion 为 null）→ 任意草稿都算有草稿；
 * - 草稿版本必须**大于**已发布版本才算有未发布变更。
 */
export function summarizeDraft(row: {
  draftVersion?: number | null;
  publishedVersion?: number | null;
}): boolean {
  const draftVersion = row.draftVersion;
  const publishedVersion = row.publishedVersion;
  if (draftVersion == null) return false;
  return publishedVersion == null || draftVersion > publishedVersion;
}

// ── 草稿 payload 校验 ────────────────────────────────────────────────

/** 各 kind 的必填 payload 字段（与后端 `validate_payload` 同口径）。 */
const REQUIRED_FIELDS: Record<DgStandardAssetKind, string[]> = {
  term: ['definition'],
  data_element: ['definition', 'data_type'],
  metric_card: ['definition', 'formula', 'unit'],
};

/** 数据元数据类型枚举（与后端一致）。 */
export const DATA_ELEMENT_DATA_TYPES = [
  'string',
  'number',
  'date',
  'boolean',
  'json',
] as const;

/** 是不是可当作 payload 的对象（null / 数组 / 标量都不算）。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

// ── 表单 → 提交 payload ──────────────────────────────────────────────

/**
 * 提交前收敛 payload：
 * - 字符串 trim 后空串丢弃；
 * - 数组只保留字符串元素，逐项 trim 后去空，空数组丢弃；
 * - 其余类型（数字/布尔/嵌套对象）原样保留；`null`/`undefined` 丢弃。
 */
export function normalizePayload(payload: unknown): Record<string, unknown> {
  if (!isRecord(payload)) return {};
  const next: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(payload)) {
    if (Array.isArray(value)) {
      const items = value
        .filter((item): item is string => typeof item === 'string')
        .map((item) => item.trim())
        .filter(Boolean);
      if (items.length) next[key] = items;
    } else if (typeof value === 'string') {
      const trimmed = value.trim();
      if (trimmed) next[key] = trimmed;
    } else if (value !== null && value !== undefined) {
      next[key] = value;
    }
  }
  return next;
}

/** 递归按键名排序，保证「同值不同键序」不会被误判成脏字段。 */
function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (isRecord(value)) {
    const next: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) next[key] = stableValue(value[key]);
    return next;
  }
  return value;
}

/**
 * 编辑表单相对服务端详情是否有未保存修改。
 *
 * 两侧都先过 `normalizePayload`：仅空白差异不算脏；清空已有字段算脏。
 * 用于「有未保存编辑时禁止发布/退役/删除」的守卫。
 */
export function isDraftDirty(
  detail: { name: string; payload: unknown },
  draft: { name: string; payload: unknown },
): boolean {
  if (detail.name.trim() !== draft.name.trim()) return true;
  return (
    JSON.stringify(stableValue(normalizePayload(detail.payload))) !==
    JSON.stringify(stableValue(normalizePayload(draft.payload)))
  );
}

/** 详情 → 列表行（去掉 payload），用于写完后就地同步抽屉的 `mode.row`。 */
export function toStandardAssetItem(detail: DgStandardAssetDetail): DgStandardAssetItem {
  return {
    assetId: detail.assetId,
    kind: detail.kind,
    code: detail.code,
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

/**
 * 校验草稿 payload 的必填字段。
 *
 * - 返回**缺失字段名**列表（空数组 = 通过）；
 * - 非字符串、空白字符串一律视为缺失；
 * - `payload` 为 null / 数组 / 标量时按空对象处理（必填项全部返回），不抛错；
 * - `data_element.data_type` 还必须是枚举值，非法值返回 `data_type:enum`。
 */
export function validateDraftFields(
  kind: DgStandardAssetKind,
  payload: unknown,
): string[] {
  const record: Record<string, unknown> = isRecord(payload) ? payload : {};
  const required = REQUIRED_FIELDS[kind] ?? [];
  const missing = required.filter((key) => {
    const value = record[key];
    return typeof value !== 'string' || !value.trim();
  });

  if (kind === 'data_element') {
    const raw = record.data_type;
    // 非字符串 / 空串已经计入 missing，这里只判「有值但不在枚举里」。
    if (
      typeof raw === 'string' &&
      raw.trim() &&
      !(DATA_ELEMENT_DATA_TYPES as readonly string[]).includes(raw.trim())
    ) {
      return [...missing, 'data_type:enum'];
    }
  }

  return missing;
}
