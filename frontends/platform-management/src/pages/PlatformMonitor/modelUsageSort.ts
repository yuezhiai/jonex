import type { Key } from 'react';
import type { PmModelUsageSort, PmModelUsageSortField } from '../../api/platformMonitor';

/** 后端缺省排序：没传 sort_by/sort_order 时就是「总 Token 降序」 */
export const PM_USAGE_DEFAULT_SORT: PmModelUsageSort = { field: 'total_tokens', order: 'desc' };

export interface UsageSortState {
  sort: PmModelUsageSort;
  /** true = 第三态「取消排序」：箭头全灭、请求不带排序参数，数据仍按后端缺省顺序 */
  cleared: boolean;
}

/** 只看用得上的字段，避免把 antd 的 SorterResult 泛型带进纯函数 */
type SorterLike = { columnKey?: Key } | { columnKey?: Key }[] | undefined;

/** 表头点击的循环：新列 → 降序；同列降序 → 升序；同列升序 → 取消（回默认、箭头全灭）。 */
export function nextUsageSortForClick(
  field: PmModelUsageSortField,
  current: PmModelUsageSort,
  cleared: boolean,
): UsageSortState {
  if (cleared) return { sort: { field, order: 'desc' }, cleared: false };
  if (field !== current.field) return { sort: { field, order: 'desc' }, cleared: false };
  if (current.order === 'desc') return { sort: { field, order: 'asc' }, cleared: false };
  return { sort: { ...PM_USAGE_DEFAULT_SORT }, cleared: true };
}

/**
 * 把 Table onChange 归约成排序状态；返回 null 表示这次交互与排序无关。
 *
 * 只有 `action === 'sort'` 才算点了表头。翻页也会进 onChange，且 sorter 里原样带着当前
 * 排序列 —— 不按 action 分流的话，翻页会被当成又点了一次表头：排序方向被翻转并跳回第一页。
 *
 * 取消排序那一下 antd 给的 sorter 不带 columnKey，用 capture 阶段记下的表头列兜底。
 */
export function resolveUsageSortChange(
  action: string,
  sorter: SorterLike,
  lastHeaderField: PmModelUsageSortField | null,
  current: PmModelUsageSort,
  cleared: boolean,
): UsageSortState | null {
  if (action !== 'sort') return null;
  const active = Array.isArray(sorter) ? sorter[0] : sorter;
  const field = (active?.columnKey as PmModelUsageSortField | undefined) ?? lastHeaderField ?? undefined;
  if (!field) return null;
  return nextUsageSortForClick(field, current, cleared);
}
