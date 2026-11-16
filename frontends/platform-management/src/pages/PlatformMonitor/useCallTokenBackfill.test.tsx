// @vitest-environment jsdom
/**
 * 调用记录 Token 补值。
 *
 * 行为约定（2026-11-10 需求方确认）：
 *  - schedule 后**不**立刻请求，等满 2 秒才发第一轮（给网关 1 秒 flush 留出余量）；
 *  - 补齐的行从下一轮的待查列表里剔除，全部到齐即停；
 *  - 最多 5 轮，之后保持 `-`（不区分「本来没调模型」与「没落库」）；
 *  - 请求失败静默重试，不改变页面状态；
 *  - 新的 schedule 取消上一轮；卸载后不再请求。
 */
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import platformZh from '../../locales/zh.json';
import platformEn from '../../locales/en.json';
import { getCallTokenUsage } from '../../api/platformMonitor';
import {
  TOKEN_BACKFILL_INTERVAL_MS,
  TOKEN_BACKFILL_MAX_ATTEMPTS,
  useCallTokenBackfill,
} from './useCallTokenBackfill';

vi.mock('../../api/platformMonitor', () => ({
  getCallTokenUsage: vi.fn(),
}));

const mockGetTokenUsage = vi.mocked(getCallTokenUsage);

/** 推进假时钟并把微任务队列排空（mock 的 promise 需要 `await` 才会落 setState）。 */
async function tick(ms: number) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  mockGetTokenUsage.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('useCallTokenBackfill', () => {
  it('满 2 秒才发第一轮，补齐后进 tokens 并停止轮询', async () => {
    mockGetTokenUsage.mockResolvedValue([{ requestId: 'req-1', totalTokens: 1234 }]);
    const { result } = renderHook(() => useCallTokenBackfill());

    act(() => {
      result.current.schedule(['req-1']);
    });
    expect(mockGetTokenUsage).not.toHaveBeenCalled();

    await tick(TOKEN_BACKFILL_INTERVAL_MS - 1);
    expect(mockGetTokenUsage).not.toHaveBeenCalled();

    await tick(1);
    expect(mockGetTokenUsage).toHaveBeenCalledTimes(1);
    expect(mockGetTokenUsage).toHaveBeenCalledWith(['req-1']);
    expect(result.current.tokens).toEqual({ 'req-1': 1234 });

    // 全部到齐 ⇒ 不再排下一轮
    await tick(TOKEN_BACKFILL_INTERVAL_MS * 3);
    expect(mockGetTokenUsage).toHaveBeenCalledTimes(1);
  });

  it('仍缺值的行下一轮只查剩下的 id', async () => {
    mockGetTokenUsage
      .mockResolvedValueOnce([
        { requestId: 'req-1', totalTokens: 10 },
        { requestId: 'req-2', totalTokens: null },
      ])
      .mockResolvedValueOnce([{ requestId: 'req-2', totalTokens: 20 }]);
    const { result } = renderHook(() => useCallTokenBackfill());

    act(() => {
      result.current.schedule(['req-1', 'req-2']);
    });

    await tick(TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.tokens).toEqual({ 'req-1': 10 });

    await tick(TOKEN_BACKFILL_INTERVAL_MS);
    expect(mockGetTokenUsage).toHaveBeenLastCalledWith(['req-2']);
    expect(result.current.tokens).toEqual({ 'req-1': 10, 'req-2': 20 });
  });

  it('一直没值就补满 5 轮后停止（保持 -）', async () => {
    mockGetTokenUsage.mockResolvedValue([]);
    const { result } = renderHook(() => useCallTokenBackfill());

    act(() => {
      result.current.schedule(['req-1']);
    });

    // 一轮一个 tick：下一轮的 setTimeout 是在 promise 落地后才排的，
    // 一次推进多轮跨度只会触发当前那一轮。
    for (let i = 0; i < TOKEN_BACKFILL_MAX_ATTEMPTS + 3; i += 1) {
      await tick(TOKEN_BACKFILL_INTERVAL_MS);
    }

    expect(mockGetTokenUsage).toHaveBeenCalledTimes(TOKEN_BACKFILL_MAX_ATTEMPTS);
    expect(result.current.tokens).toEqual({});
  });

  it('请求失败静默重试，不影响页面', async () => {
    mockGetTokenUsage
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce([{ requestId: 'req-1', totalTokens: 7 }]);
    const { result } = renderHook(() => useCallTokenBackfill());

    act(() => {
      result.current.schedule(['req-1']);
    });

    await tick(TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.tokens).toEqual({});

    await tick(TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.tokens).toEqual({ 'req-1': 7 });
  });

  it('新的 schedule（翻页 / 改筛选）取消上一轮', async () => {
    mockGetTokenUsage.mockResolvedValue([]);
    const { result } = renderHook(() => useCallTokenBackfill());

    act(() => {
      result.current.schedule(['req-old']);
    });
    act(() => {
      result.current.schedule(['req-new']);
    });

    await tick(TOKEN_BACKFILL_INTERVAL_MS);
    expect(mockGetTokenUsage).toHaveBeenCalledTimes(1);
    expect(mockGetTokenUsage).toHaveBeenLastCalledWith(['req-new']);
  });

  it('传空数组 = 取消本轮且不发请求', async () => {
    mockGetTokenUsage.mockResolvedValue([]);
    const { result } = renderHook(() => useCallTokenBackfill());

    act(() => {
      result.current.schedule(['req-1']);
    });
    act(() => {
      result.current.schedule([]);
    });

    await tick(TOKEN_BACKFILL_INTERVAL_MS * 2);
    expect(mockGetTokenUsage).not.toHaveBeenCalled();
  });

  it('卸载后不再请求', async () => {
    mockGetTokenUsage.mockResolvedValue([]);
    const { result, unmount } = renderHook(() => useCallTokenBackfill());

    act(() => {
      result.current.schedule(['req-1']);
    });
    unmount();

    await tick(TOKEN_BACKFILL_INTERVAL_MS * 3);
    expect(mockGetTokenUsage).not.toHaveBeenCalled();
  });

  it('Token 列名在 zh / en 均已配置', () => {
    const zh = platformZh.platformMonitor.records.column as Record<string, string>;
    const en = platformEn.platformMonitor.records.column as Record<string, string>;

    expect(zh.tokens).toBe('Token 用量');
    expect(en.tokens.trim()).not.toBe('');
  });
});
