/**
 * 「数据资产对齐」区的纯逻辑（无 React / 无 DOM），单独成文件便于直接单测。
 *
 * 与 `focusModel.ts` 同一定位：组件只管渲染与请求，分组顺序、计数归一这类
 * 「会静默出错」的规则放这里钉死。
 */
import type {
  AlignmentSuggestionResult,
  OntologyDataAlignment,
} from '@/api/ontologyAlignment';

/** 三种合法状态在界面上的固定展示顺序（候选 → 已启用 → 已拒绝）。 */
export const ALIGNMENT_STATUS_ORDER = ['proposed', 'active', 'rejected'] as const;

export interface AlignmentStatusGroup {
  /**
   * 后端状态字符串。已知三态按固定顺序；**未知值原样保留**（排在已知三态
   * 之后、按首见顺序）—— 将来后端扩展状态时不能把行悄悄吞掉。
   */
  status: string;
  items: OntologyDataAlignment[];
}

/**
 * 按状态分组（保序）。
 *
 * - 组顺序固定为 `proposed → active → rejected`，与列表内容的先后无关；
 * - 组内保持入参顺序（后端已按更新时间排过，前端不再排）；
 * - 空组不产出；缺 `status` 按 `proposed` 处理（后端列有默认值，缺了多半是
 *   老数据 —— 显示成「候选」至少还能被审批，丢掉则永远没人看见）。
 */
export function groupByStatus(items: OntologyDataAlignment[]): AlignmentStatusGroup[] {
  const buckets = new Map<string, OntologyDataAlignment[]>();
  for (const item of items || []) {
    const status = item.status || 'proposed';
    const bucket = buckets.get(status);
    if (bucket) bucket.push(item);
    else buckets.set(status, [item]);
  }
  const known = (ALIGNMENT_STATUS_ORDER as readonly string[]).filter((s) => buckets.has(s));
  const unknown = Array.from(buckets.keys()).filter(
    (s) => !(ALIGNMENT_STATUS_ORDER as readonly string[]).includes(s),
  );
  return [...known, ...unknown].map((status) => ({
    status,
    items: buckets.get(status) ?? [],
  }));
}

export interface SuggestionSummary {
  created: number;
  existing: number;
  unresolved: number;
}

/** 计数归一：缺项 / 非数字 / NaN / 负数一律归 0 —— 面板上不显示 `NaN` 或 `-1`。 */
function count(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
}

/** 生成建议回执 → 面板一行摘要用的计数（组件再套 i18n 文案）。 */
export function suggestionSummary(
  result?: Partial<AlignmentSuggestionResult> | null,
): SuggestionSummary {
  return {
    created: count(result?.created),
    existing: count(result?.existing),
    unresolved: count(result?.unresolved),
  };
}
