/**
 * 回归测试：静默重取（refresh({ silent: true })）顶掉仍在飞的首屏取数时，
 * loading 必须被解除。
 *
 * 真实故障路径（2026-11-17 反馈）：从知识库列表的「情况追踪」直接进入详情页
 * （?tab=tracking），文档库页签此时是首次挂载 —— kbType 已就绪，jonex_wiki 的
 * 编译状态轮询与文档列表同时发起。轮询先回 → DocumentLibrary 对表格做了一次
 * silent 重取，于是「首屏加载」被静默请求取代；两条请求收尾时都按 silent 跳过
 * setLoading(false)，表格遮罩再也没人摘掉，页面一直转圈。
 *
 * 该 hook 属于 @jonex/shared-lib，但前端只有 core-business 配了 vitest，故测试放这里，
 * 直接引用 shared-lib 源码（不走 dist，避免验证的是旧构建产物）。
 */
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useTableQuery } from '../../../shared/shared-lib/src/components/CommonTable/useTableQuery';

type Row = { id: string };
type Filters = { keyword?: string };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

describe('useTableQuery silent refresh', () => {
  it('静默重取顶掉首屏取数后，loading 必须解除', async () => {
    const first = deferred<{ list: Row[]; total: number }>();
    const silent = deferred<{ list: Row[]; total: number }>();
    const fetch = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(silent.promise)
      .mockResolvedValue({ list: [], total: 0 });

    const { result } = renderHook(() =>
      useTableQuery<Row, Filters>({ fetch, paginated: true, defaultPageSize: 10 }),
    );

    // 首屏请求在飞：遮罩已亮，且此时还没有数据
    expect(result.current.loading).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);

    // 轮询触发的静默重取（例如 Wiki 编译状态先回）
    act(() => {
      result.current.refresh({ silent: true });
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    // 静默本身不该重新点亮遮罩，但此时遮罩本来就亮着
    expect(result.current.loading).toBe(true);

    // 静默请求先回，旧的首屏请求后回（被序号丢弃）
    await act(async () => {
      silent.resolve({ list: [{ id: 'fresh' }], total: 1 });
    });
    await act(async () => {
      first.resolve({ list: [{ id: 'stale' }], total: 1 });
    });

    // 最新一次取数已经落地，遮罩必须解除；否则页面永远停在加载态
    expect(result.current.loading).toBe(false);
    expect(result.current.list).toEqual([{ id: 'fresh' }]);
  });

  it('静默重取失败也不该把 loading 留在 true', async () => {
    const first = deferred<{ list: Row[]; total: number }>();
    const silent = deferred<{ list: Row[]; total: number }>();
    const fetch = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(silent.promise)
      .mockResolvedValue({ list: [], total: 0 });
    const onError = vi.fn();

    const { result } = renderHook(() =>
      useTableQuery<Row, Filters>({ fetch, paginated: true, defaultPageSize: 10, onError }),
    );

    act(() => {
      result.current.refresh({ silent: true });
    });

    await act(async () => {
      silent.resolve({ list: [{ id: 'fresh' }], total: 1 });
    });
    await act(async () => {
      first.resolve({ list: [{ id: 'stale' }], total: 1 });
    });

    expect(result.current.loading).toBe(false);
  });
});
