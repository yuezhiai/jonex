import type { ReactNode } from 'react';

/**
 * 明细表上方的筛选栏，只负责让控件横排并留间距、窄屏可换行。
 *
 * 曾有一个 `right` 插槽（放「共 N 条记录」计数），随调用记录改用 CommonTable 而移除 ——
 * 那张表的总数由 CommonTable 自带的分页器显示，不必再加一处第二来源。
 */
export default function FilterBar({ children }: { children: ReactNode }) {
  return <div className="yx-pm-filter-bar">{children}</div>;
}
