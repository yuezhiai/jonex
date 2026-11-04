/**
 * 模型用量明细的排序归约。
 *
 * 重点回归：翻页也会触发 Table 的 onChange，且 sorter 里带着当前排序列 ——
 * 不能被当成又点了一次表头（否则翻页会把排序方向翻转并跳回第一页）。
 */
import { describe, expect, it } from 'vitest';
import { PM_USAGE_DEFAULT_SORT, nextUsageSortForClick, resolveUsageSortChange } from './modelUsageSort';

const DESC_CALLS = { field: 'call_count', order: 'desc' } as const;
const ASC_CALLS = { field: 'call_count', order: 'asc' } as const;

describe('resolveUsageSortChange', () => {
  it('翻页回调即使带着当前排序列也不改排序', () => {
    expect(resolveUsageSortChange('paginate', { columnKey: 'call_count' }, 'call_count', DESC_CALLS, false)).toBeNull();
    expect(resolveUsageSortChange('paginate', { columnKey: 'call_count' }, 'call_count', ASC_CALLS, false)).toBeNull();
  });

  it('取消排序后再翻页，也不会把排序复活', () => {
    expect(resolveUsageSortChange('paginate', undefined, 'call_count', PM_USAGE_DEFAULT_SORT, true)).toBeNull();
  });

  it('筛选变化不改排序', () => {
    expect(resolveUsageSortChange('filter', { columnKey: 'call_count' }, null, DESC_CALLS, false)).toBeNull();
  });

  it('点表头才改排序：新列降序、同列降序转升序、同列升序转取消', () => {
    expect(
      resolveUsageSortChange('sort', { columnKey: 'call_count' }, 'call_count', PM_USAGE_DEFAULT_SORT, false),
    ).toEqual({
      sort: { field: 'call_count', order: 'desc' },
      cleared: false,
    });
    expect(resolveUsageSortChange('sort', { columnKey: 'call_count' }, 'call_count', DESC_CALLS, false)).toEqual({
      sort: { field: 'call_count', order: 'asc' },
      cleared: false,
    });
    expect(resolveUsageSortChange('sort', { columnKey: 'call_count' }, 'call_count', ASC_CALLS, false)).toEqual({
      sort: { ...PM_USAGE_DEFAULT_SORT },
      cleared: true,
    });
  });

  it('取消排序那一下 pager 不带 columnKey，用 capture 记下的列兜底', () => {
    expect(resolveUsageSortChange('sort', undefined, 'call_count', DESC_CALLS, false)).toEqual({
      sort: { field: 'call_count', order: 'asc' },
      cleared: false,
    });
  });

  it('取不到列时不做任何事', () => {
    expect(resolveUsageSortChange('sort', { columnKey: undefined }, null, DESC_CALLS, false)).toBeNull();
  });

  it('多列 sorter 只认第一项', () => {
    expect(
      resolveUsageSortChange(
        'sort',
        [{ columnKey: 'prompt_tokens' }, { columnKey: 'call_count' }],
        null,
        PM_USAGE_DEFAULT_SORT,
        false,
      ),
    ).toEqual({ sort: { field: 'prompt_tokens', order: 'desc' }, cleared: false });
  });
});

describe('nextUsageSortForClick', () => {
  it('取消态下点任意列，从降序重新开始', () => {
    expect(nextUsageSortForClick('model', PM_USAGE_DEFAULT_SORT, true)).toEqual({
      sort: { field: 'model', order: 'desc' },
      cleared: false,
    });
  });

  it('默认排序是总 Token 降序', () => {
    expect(PM_USAGE_DEFAULT_SORT).toEqual({ field: 'total_tokens', order: 'desc' });
  });
});
