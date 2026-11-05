// @vitest-environment jsdom
import React from 'react';
import { message } from 'antd';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MockInstance } from 'vitest';

// t 桩：带插值参数时把参数拼进返回值，便于断言百分比等动态值。
const { translate } = vi.hoisted(() => ({
  translate: (key: string, opts?: Record<string, unknown>) =>
    opts && Object.keys(opts).length
      ? `${key} ${Object.entries(opts)
          .map(([k, v]) => `${k}=${String(v)}`)
          .join(' ')}`
      : key,
}));

vi.mock('react-i18next', () => ({
  // 稳定引用：组件 useEffect 依赖 [t]，每次渲染都换引用会导致反复重载。
  useTranslation: () => ({ t: translate }),
}));

vi.mock('@/api/domainKnowledge', () => ({
  getSimilaritySuggestions: vi.fn(),
  mergeSimilaritySuggestion: vi.fn(),
  dismissSimilaritySuggestion: vi.fn(),
}));

import {
  dismissSimilaritySuggestion,
  getSimilaritySuggestions,
  mergeSimilaritySuggestion,
} from '@/api/domainKnowledge';
import type { SimilaritySuggestion } from '@/types/domainKnowledge';
import SimilaritySuggestionsBanner from './SimilaritySuggestionsBanner';

type Suggestions = Awaited<ReturnType<typeof getSimilaritySuggestions>>;

const mockList = vi.mocked(getSimilaritySuggestions);
const mockMerge = vi.mocked(mergeSimilaritySuggestion);
const mockDismiss = vi.mocked(dismissSimilaritySuggestion);

function suggestion(overrides: Partial<SimilaritySuggestion> = {}): SimilaritySuggestion {
  return {
    id: 's-1',
    targetDocumentId: 'doc-target',
    targetFileName: '目标文档.pdf',
    similarity: 0.9,
    method: 'hash',
    canMerge: true,
    blockReason: null,
    sourceBlockReason: null,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type MessageMock = MockInstance<Parameters<typeof message.success>, ReturnType<typeof message.success>>;

let successSpy: MessageMock;
let errorSpy: MessageMock;

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
});

beforeEach(() => {
  mockList.mockReset();
  mockMerge.mockReset();
  mockDismiss.mockReset();
  mockList.mockResolvedValue([] as Suggestions);
  successSpy = vi.spyOn(message, 'success').mockReturnValue(undefined as never);
  errorSpy = vi.spyOn(message, 'error').mockReturnValue(undefined as never);
});

afterEach(() => {
  cleanup();
  successSpy.mockRestore();
  errorSpy.mockRestore();
});

interface RenderOverrides {
  docId?: string;
  canWrite?: boolean;
  refreshKey?: string;
  onMerged?: () => void;
}

function renderBanner(overrides: RenderOverrides = {}) {
  return render(
    React.createElement(SimilaritySuggestionsBanner, {
      docId: overrides.docId ?? 'doc-src',
      canWrite: overrides.canWrite ?? true,
      refreshKey: overrides.refreshKey,
      onMerged: overrides.onMerged ?? (() => undefined),
    }),
  );
}

async function waitForBannerText(): Promise<HTMLElement> {
  await waitFor(() => expect(screen.queryByText(/domainKnowledge\.similarity\.banner/)).not.toBeNull());
  return screen.getByText(/domainKnowledge\.similarity\.banner/);
}

describe('SimilaritySuggestionsBanner', () => {
  it('无建议时不渲染任何内容', async () => {
    mockList.mockResolvedValue([]);
    const { container } = renderBanner();

    await waitFor(() => expect(mockList).toHaveBeenCalledWith('doc-src'));
    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it('加载失败时不渲染，也不弹全局错误', async () => {
    mockList.mockRejectedValue(new Error('load boom'));
    const { container } = renderBanner();

    await waitFor(() => expect(mockList).toHaveBeenCalledWith('doc-src'));
    await waitFor(() => expect(container.firstChild).toBeNull());
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it('有建议时显示文案、目标文件名与百分比', async () => {
    mockList.mockResolvedValue([suggestion({ targetFileName: '甲.pdf', similarity: 0.926 })]);
    renderBanner();

    const text = await waitForBannerText();
    expect(text.textContent).toContain('name=甲.pdf');
    expect(text.textContent).toContain('percent=93');
  });

  it('refreshKey 变化时重新拉取建议', async () => {
    mockList.mockResolvedValue([]);
    const { rerender } = renderBanner();

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(1));

    rerender(
      React.createElement(SimilaritySuggestionsBanner, {
        docId: 'doc-src',
        canWrite: true,
        refreshKey: 'updated-at-2',
        onMerged: () => undefined,
      }),
    );

    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2));
  });

  it('canMerge=false 时合并按钮禁用，并用 sourceBlockReason 显示原因', async () => {
    mockList.mockResolvedValue([suggestion({ canMerge: false, blockReason: null, sourceBlockReason: 'compiling' })]);
    renderBanner();

    await waitForBannerText();
    const mergeButton = screen.getByRole('button', {
      name: 'domainKnowledge.similarity.merge',
    }) as HTMLButtonElement;
    expect(mergeButton.disabled).toBe(true);

    // blockReason 为空时回退到 sourceBlockReason；枚举码映射为可读文案
    const reason = screen.getByText(/domainKnowledge\.similarity\.notCompiled/);
    expect(reason.textContent).toContain('state=domainKnowledge.similarity.state.compiling');
  });

  it('未知 block_reason 原样展示', async () => {
    mockList.mockResolvedValue([suggestion({ canMerge: false, blockReason: 'legacy_code', sourceBlockReason: null })]);
    renderBanner();

    const reason = await waitFor(() => screen.getByText(/domainKnowledge\.similarity\.notCompiled/));
    expect(reason.textContent).toContain('state=legacy_code');
  });

  it('canMerge=false 且两侧 reason 均为空时不显示原因', async () => {
    mockList.mockResolvedValue([suggestion({ canMerge: false, blockReason: null, sourceBlockReason: null })]);
    renderBanner();

    await waitForBannerText();
    expect(screen.queryByText(/domainKnowledge\.similarity\.notCompiled/)).toBeNull();
    const mergeButton = screen.getByRole('button', {
      name: 'domainKnowledge.similarity.merge',
    }) as HTMLButtonElement;
    expect(mergeButton.disabled).toBe(true);
  });

  it('点击合并调用 API 并触发 onMerged', async () => {
    mockList.mockResolvedValue([suggestion()]);
    mockMerge.mockResolvedValue({ targetDocumentId: 'doc-target', versionNo: 3 });
    const onMerged = vi.fn();
    renderBanner({ onMerged });

    await waitForBannerText();
    fireEvent.click(screen.getByRole('button', { name: 'domainKnowledge.similarity.merge' }));

    await waitFor(() => expect(mockMerge).toHaveBeenCalledWith('doc-src', 's-1'));
    await waitFor(() => expect(onMerged).toHaveBeenCalledTimes(1));
    expect(successSpy).toHaveBeenCalled();
  });

  it('合并失败时不触发 onMerged，条目保留', async () => {
    mockList.mockResolvedValue([suggestion()]);
    mockMerge.mockRejectedValueOnce(new Error('merge boom'));
    const onMerged = vi.fn();
    renderBanner({ onMerged });

    await waitForBannerText();
    fireEvent.click(screen.getByRole('button', { name: 'domainKnowledge.similarity.merge' }));

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('merge boom'));
    expect(onMerged).not.toHaveBeenCalled();
    expect(screen.queryByText(/domainKnowledge\.similarity\.banner/)).not.toBeNull();
  });

  it('合并后重新解析失败时给出提示，但仍触发 onMerged', async () => {
    mockList.mockResolvedValue([suggestion()]);
    mockMerge.mockResolvedValue({
      targetDocumentId: 'doc-target',
      versionNo: 4,
      reparseError: 'parse boom',
    });
    const onMerged = vi.fn();
    renderBanner({ onMerged });

    await waitForBannerText();
    fireEvent.click(screen.getByRole('button', { name: 'domainKnowledge.similarity.merge' }));

    await waitFor(() => expect(onMerged).toHaveBeenCalledTimes(1));
    expect(errorSpy).toHaveBeenCalledWith('domainKnowledge.similarity.mergeReparseFailed');
    expect(successSpy).not.toHaveBeenCalled();
    // 合并成功后条目已从 UI 移除
    expect(screen.queryByText(/domainKnowledge\.similarity\.banner/)).toBeNull();
  });

  it('点击忽略后该条从 UI 消失', async () => {
    mockList.mockResolvedValue([suggestion({ targetFileName: '乙.pdf' })]);
    mockDismiss.mockResolvedValue(undefined);
    renderBanner();

    const text = await waitForBannerText();
    expect(text.textContent).toContain('乙.pdf');

    fireEvent.click(screen.getByRole('button', { name: 'domainKnowledge.similarity.dismiss' }));

    await waitFor(() => expect(mockDismiss).toHaveBeenCalledWith('doc-src', 's-1'));
    await waitFor(() => expect(screen.queryByText(/domainKnowledge\.similarity\.banner/)).toBeNull());
  });

  it('忽略失败时条目保留', async () => {
    mockList.mockResolvedValue([suggestion()]);
    mockDismiss.mockRejectedValueOnce(new Error('dismiss boom'));
    renderBanner();

    await waitForBannerText();
    fireEvent.click(screen.getByRole('button', { name: 'domainKnowledge.similarity.dismiss' }));

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('dismiss boom'));
    expect(screen.queryByText(/domainKnowledge\.similarity\.banner/)).not.toBeNull();
  });

  it('多条时操作中其余按钮禁用，且只有被点按钮转圈', async () => {
    const pending = deferred<Awaited<ReturnType<typeof mergeSimilaritySuggestion>>>();
    mockList.mockResolvedValue([
      suggestion({ id: 'a', targetFileName: 'A.pdf' }),
      suggestion({ id: 'b', targetFileName: 'B.pdf' }),
    ]);
    mockMerge.mockReturnValueOnce(pending.promise);
    renderBanner();

    await waitFor(() => expect(screen.getAllByText(/domainKnowledge\.similarity\.banner/).length).toBe(2));
    const mergeButtons = screen.getAllByRole('button', {
      name: 'domainKnowledge.similarity.merge',
    }) as HTMLButtonElement[];

    fireEvent.click(mergeButtons[0]);

    await waitFor(() => expect(mockMerge).toHaveBeenCalledWith('doc-src', 'a'));
    await waitFor(() => expect(mergeButtons[1].disabled).toBe(true));
    // 第一条（被点）进入 loading，第二条只禁用不转圈
    expect(mergeButtons[0].className).toContain('ant-btn-loading');
    expect(mergeButtons[1].className).not.toContain('ant-btn-loading');

    await act(async () => {
      pending.resolve({ targetDocumentId: 'doc-target', versionNo: 1 });
    });
  });

  it('canWrite=false 时不显示操作按钮', async () => {
    mockList.mockResolvedValue([suggestion()]);
    renderBanner({ canWrite: false });

    await waitForBannerText();
    expect(screen.queryByRole('button', { name: 'domainKnowledge.similarity.merge' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'domainKnowledge.similarity.dismiss' })).toBeNull();
  });

  it('存在不可合并条目时每 10s 轮询，全部可合并则不轮询', async () => {
    vi.useFakeTimers();
    try {
      mockList.mockResolvedValue([suggestion({ canMerge: false, blockReason: 'compiling' })]);
      const { unmount } = renderBanner();

      await act(async () => {});
      expect(mockList).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(10_000);
      });
      expect(mockList).toHaveBeenCalledTimes(2);

      // 卸载后停止轮询
      unmount();
      await act(async () => {
        vi.advanceTimersByTime(30_000);
      });
      expect(mockList).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('全部可合并时不开启轮询', async () => {
    vi.useFakeTimers();
    try {
      mockList.mockResolvedValue([suggestion()]);
      renderBanner();

      await act(async () => {});
      expect(mockList).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(30_000);
      });
      expect(mockList).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
