/**
 * W2.6 资源视图展示工具（纯函数，无 React / i18next 依赖）。
 *
 * 约定：本文件只接收 camelCase 数据；需要本地化的返回值返回 **i18n 键**
 * （`dataGovernance.resourceViews.*`），由页面交给 `t()` 解析，方便单测。
 */
import type {
  DgResourceSensitivity,
  DgSourceAnchorResult,
  DgStandardBinding,
} from './types';

/** 后端对空 domain 的归一值。 */
export const UNASSIGNED_DOMAIN = '__unassigned__';
/** 后端对空 owner 的归一值。 */
export const UNOWNED_OWNER = '__unowned__';

/** 未分配分组的 i18n 键（i18n 防漂移测试会扫描静态 key）。 */
export const UNASSIGNED_DOMAIN_KEY = 'dataGovernance.resourceViews.unassignedDomain';
export const UNOWNED_OWNER_KEY = 'dataGovernance.resourceViews.unownedOwner';

/**
 * 业务域展示标签：空值 / `__unassigned__` → i18n 键；其余原样返回。
 *
 * 页面用法：`const label = domainLabel(row.domain); t(label)`。
 */
export function domainLabel(domain: string | null | undefined): string {
  const value = (domain ?? '').trim();
  if (!value || value === UNASSIGNED_DOMAIN) return UNASSIGNED_DOMAIN_KEY;
  return value;
}

/**
 * 责任人展示标签：优先平台侧 display_name，空值 / `__unowned__` → i18n 键。
 *
 * owner 匹配用户表只影响展示，不改变归属；未知 owner 原样返回。
 */
export function ownerLabel(
  owner: string | null | undefined,
  displayName?: string | null,
): string {
  const name = (displayName ?? '').trim();
  if (name && name !== UNOWNED_OWNER) return name;
  const value = (owner ?? '').trim();
  if (!value || value === UNOWNED_OWNER) return UNOWNED_OWNER_KEY;
  return value;
}

/**
 * 汇总多组 / 多行的分级计数（业务域卡片与责任表合计共用）。
 *
 * 未知级别不计数（与后端固定五档口径一致）；缺失字段按 0。
 */
export function sensitivityTotals(
  items: ReadonlyArray<
    | { sensitivity?: Partial<DgResourceSensitivity> | null }
    | null
    | undefined
  >,
): DgResourceSensitivity {
  const total: DgResourceSensitivity = {
    public: 0,
    internal: 0,
    confidential: 0,
    secret: 0,
    unclassified: 0,
  };
  for (const item of items) {
    const value = item?.sensitivity;
    if (!value) continue;
    for (const key of Object.keys(total) as (keyof DgResourceSensitivity)[]) {
      const count = value[key];
      if (typeof count === 'number' && Number.isFinite(count)) {
        total[key] += count;
      }
    }
  }
  return total;
}

/**
 * 覆盖率展示：后端给 0..1 比率，转成百分比字符串；null / 非数 → `—`。
 *
 * 最多一位小数，整数不带 `.0`（`0.5 → "50%"`、`0.826 → "82.6%"`）。
 */
export function coveragePercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const percent = Math.round(value * 1000) / 10;
  return `${percent}%`;
}

export type DgAnchorState = 'linked' | 'unresolved' | 'degraded';

/**
 * 原文锚点三态：`degraded`（上游失败）> `unresolved`（无对齐/实体/文档）
 * > `linked`。`null` 视为 unresolved（尚未取到数据，不冒充已关联）。
 */
export function anchorState(
  result:
    | Pick<DgSourceAnchorResult, 'linked' | 'unresolved' | 'degraded'>
    | null
    | undefined,
): DgAnchorState {
  if (!result) return 'unresolved';
  if (result.degraded) return 'degraded';
  if (result.unresolved || !Array.isArray(result.linked) || result.linked.length === 0) {
    return 'unresolved';
  }
  return 'linked';
}

/**
 * 绑定行摘要：`{standardKind}:{standardCode} → {assetId}`。
 *
 * 仅用于列表 / 抽屉的一行说明；缺字段时用 `—` 占位，不抛错。
 */
export function bindingSummary(
  binding:
    | Pick<DgStandardBinding, 'standardKind' | 'standardCode' | 'assetType' | 'assetId'>
    | null
    | undefined,
): string {
  if (!binding) return '';
  const kind = (binding.standardKind ?? '').trim();
  const code = (binding.standardCode ?? '').trim();
  const assetId = (binding.assetId ?? '').trim();
  const left = kind && code ? `${kind}:${code}` : (kind || code);
  if (!left && !assetId) return '';
  return `${left || '—'} → ${assetId || '—'}`;
}
