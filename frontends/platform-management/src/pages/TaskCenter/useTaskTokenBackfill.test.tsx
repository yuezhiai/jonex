// @vitest-environment jsdom
/**
 * 任务中心 Token 补值（按文档归集）。
 *
 * 与调用记录那处同源的五条不变量：2 秒首轮、2 秒 × 5 上限、代次防串页、失败静默、
 * 不覆盖已知值。任务中心另有两点：
 *
 *  - 值是 `{documentId, documentName, totalTokens}`，不是单个数字；
 *  - **运行中任务每轮都重查**（Token 随任务推进增长），`document_id === null`
 *    （非知识库文档任务）视为已解析。
 */
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import platformZh from '../../locales/zh.json';
import platformEn from '../../locales/en.json';
import { getTaskTokenUsage } from '../../api/taskCenter';
import {
  TASK_TOKEN_BACKFILL_BATCH,
  TASK_TOKEN_BACKFILL_INTERVAL_MS,
  TASK_TOKEN_BACKFILL_MAX_ATTEMPTS,
  useTaskTokenBackfill,
} from './useTaskTokenBackfill';

vi.mock('../../api/taskCenter', () => ({
  getTaskTokenUsage: vi.fn(),
}));

const mockGetTokenUsage = vi.mocked(getTaskTokenUsage);

/** 造一行接口返回（snake_case，与 DTO 逐字对齐）。 */
const item = (
  taskId: string,
  documentId: string | null,
  totalTokens: number | null,
  documentName: string | null = null,
) => ({
  task_id: taskId,
  document_id: documentId,
  document_name: documentName,
  total_tokens: totalTokens,
});

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

describe('useTaskTokenBackfill', () => {
  it('满 2 秒才发第一轮，终态任务拿到值即停', async () => {
    mockGetTokenUsage.mockResolvedValue([item('t1', 'd1', 29779, 'a.md')]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1']);
    });
    expect(mockGetTokenUsage).not.toHaveBeenCalled();

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS - 1);
    expect(mockGetTokenUsage).not.toHaveBeenCalled();

    await tick(1);
    expect(mockGetTokenUsage).toHaveBeenCalledWith(['t1']);
    expect(result.current.usage).toEqual({
      t1: {
        documentId: 'd1',
        documentName: 'a.md',
        totalTokens: 29779,
        parseTokens: null,
        ontologyTokens: null,
        wikiTokens: null,
      kbCompileTokens: null,
      batchSize: null,
      },
    });

    // 全部到齐 ⇒ 不再排下一轮
    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS * 3);
    expect(mockGetTokenUsage).toHaveBeenCalledTimes(1);
  });

  it('运行中的任务每轮都重查，数字递增', async () => {
    mockGetTokenUsage
      .mockResolvedValueOnce([item('t1', 'd1', 100, 'a.md')])
      .mockResolvedValueOnce([item('t1', 'd1', 250, 'a.md')]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1'], new Set(['t1']));
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.usage.t1.totalTokens).toBe(100);

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(mockGetTokenUsage).toHaveBeenCalledTimes(2);
    expect(result.current.usage.t1.totalTokens).toBe(250);
  });

  it('后来的 null 不覆盖已知值', async () => {
    mockGetTokenUsage
      .mockResolvedValueOnce([item('t1', 'd1', 100, 'a.md')])
      .mockResolvedValueOnce([item('t1', 'd1', null, null)]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1'], new Set(['t1']));
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);

    // 运行中收到 null（比如计量还没落库）不能把已经显示的 100 抹掉
    expect(result.current.usage.t1).toEqual({
      documentId: 'd1',
      documentName: 'a.md',
      totalTokens: 100,
      parseTokens: null,
      ontologyTokens: null,
      wikiTokens: null,
      kbCompileTokens: null,
      batchSize: null,
    });
  });

  it('非知识库任务（document_id 为 null）一轮即止，不再空转', async () => {
    mockGetTokenUsage.mockResolvedValue([item('t1', null, null)]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    // 即使它在运行中也不该继续查：没有文档映射，再查也不会有 Token
    act(() => {
      result.current.schedule(['t1'], new Set(['t1']));
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.usage.t1).toEqual({
      documentId: null,
      documentName: null,
      totalTokens: null,
      parseTokens: null,
      ontologyTokens: null,
      wikiTokens: null,
      kbCompileTokens: null,
      batchSize: null,
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS * 3);
    expect(mockGetTokenUsage).toHaveBeenCalledTimes(1);
  });

  it('整库编译段与批次大小原样透传（wiki 库）', async () => {
    mockGetTokenUsage.mockResolvedValue([
      {
        task_id: 't1',
        document_id: 'd1',
        document_name: 'a.md',
        total_tokens: 3210,
        parse_tokens: 400,
        ontology_tokens: 600,
        wiki_tokens: 0,
        kb_compile_tokens: 2210,
        batch_size: 4,
      },
    ]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1']);
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.usage.t1).toEqual({
      documentId: 'd1',
      documentName: 'a.md',
      totalTokens: 3210,
      parseTokens: 400,
      ontologyTokens: 600,
      wikiTokens: 0,
      kbCompileTokens: 2210,
      batchSize: 4,
    });
  });

  it('整库段后来的 null 不覆盖已知值', async () => {
    mockGetTokenUsage
      .mockResolvedValueOnce([
        {
          task_id: 't1',
          document_id: 'd1',
          document_name: 'a.md',
          total_tokens: 100,
          parse_tokens: 100,
          ontology_tokens: 0,
          wiki_tokens: 0,
          kb_compile_tokens: 50,
          batch_size: 2,
        },
      ])
      .mockResolvedValue([]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1'], new Set(['t1']));
    });
    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.usage.t1.kbCompileTokens).toBe(50);

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.usage.t1.kbCompileTokens).toBe(50);
    expect(result.current.usage.t1.batchSize).toBe(2);
  });

  it('终态任务一直没值就补满 5 轮后停止（保持 -）', async () => {
    mockGetTokenUsage.mockResolvedValue([item('t1', 'd1', null, 'a.md')]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1']);
    });

    // 一轮一个 tick：下一轮的 setTimeout 是在 promise 落地后才排的
    for (let i = 0; i < TASK_TOKEN_BACKFILL_MAX_ATTEMPTS + 3; i += 1) {
      await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    }

    expect(mockGetTokenUsage).toHaveBeenCalledTimes(TASK_TOKEN_BACKFILL_MAX_ATTEMPTS);
    // 文档名补到了、Token 仍是 null —— 不能伪装成 0
    expect(result.current.usage.t1.totalTokens).toBeNull();
  });

  it('超过 100 个任务按批并发请求', async () => {
    mockGetTokenUsage.mockResolvedValue([]);
    const { result } = renderHook(() => useTaskTokenBackfill());
    const ids = Array.from({ length: TASK_TOKEN_BACKFILL_BATCH * 2 + 30 }, (_, i) => `t${i}`);

    act(() => {
      result.current.schedule(ids);
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);

    expect(mockGetTokenUsage).toHaveBeenCalledTimes(3);
    const sizes = mockGetTokenUsage.mock.calls.map(([batch]) => batch.length);
    expect(sizes).toEqual([TASK_TOKEN_BACKFILL_BATCH, TASK_TOKEN_BACKFILL_BATCH, 30]);
  });

  it('请求失败静默重试，不影响页面', async () => {
    mockGetTokenUsage
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce([item('t1', 'd1', 7, 'a.md')]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1']);
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.usage).toEqual({});

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(result.current.usage.t1.totalTokens).toBe(7);
  });

  it('新的 schedule（翻页 / 改筛选）取消上一轮', async () => {
    mockGetTokenUsage.mockResolvedValue([]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t-old']);
    });
    act(() => {
      result.current.schedule(['t-new']);
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS);
    expect(mockGetTokenUsage).toHaveBeenCalledTimes(1);
    expect(mockGetTokenUsage).toHaveBeenLastCalledWith(['t-new']);
  });

  it('传空数组 = 取消本轮且不发请求', async () => {
    mockGetTokenUsage.mockResolvedValue([]);
    const { result } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1']);
    });
    act(() => {
      result.current.schedule([]);
    });

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS * 2);
    expect(mockGetTokenUsage).not.toHaveBeenCalled();
  });

  it('卸载后不再请求', async () => {
    mockGetTokenUsage.mockResolvedValue([]);
    const { result, unmount } = renderHook(() => useTaskTokenBackfill());

    act(() => {
      result.current.schedule(['t1']);
    });
    unmount();

    await tick(TASK_TOKEN_BACKFILL_INTERVAL_MS * 3);
    expect(mockGetTokenUsage).not.toHaveBeenCalled();
  });

  it('列名与详情文案在 zh / en 均已配置', () => {
    const zhColumn = platformZh.taskCenter.column as Record<string, string>;
    const enColumn = platformEn.taskCenter.column as Record<string, string>;
    const zhDetail = platformZh.taskCenter.detail as Record<string, string>;
    const enDetail = platformEn.taskCenter.detail as Record<string, string>;

    expect(zhColumn.tokens).toBe('Token 消耗');
    expect(enColumn.tokens.trim()).not.toBe('');
    for (const key of ['tokenUsage', 'documentName', 'tokens', 'tokenNotApplicable']) {
      expect(zhDetail[key]?.trim()).toBeTruthy();
      expect(enDetail[key]?.trim()).toBeTruthy();
    }
  });
});
