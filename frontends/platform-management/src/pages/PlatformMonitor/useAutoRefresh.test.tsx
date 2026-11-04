// @vitest-environment jsdom
/**
 * 平台监控页的自动刷新 hook。
 *
 * 行为约定：
 *  - 挂载后不立即取数，等满一个周期才触发；
 *  - 每满 60 秒触发一次，触发后重新计时；
 *  - 页面在后台（`document.hidden`）时暂停，回到前台立即触发一次并重新计时；
 *  - 手动刷新调用 `reset()` 后重新计时；
 *  - 卸载后不再触发。
 */
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import platformZh from '../../locales/zh.json';
import platformEn from '../../locales/en.json';
import { PM_AUTO_REFRESH_MS, useAutoRefresh } from './useAutoRefresh';

/** jsdom 里 `document.hidden` 只读，用自有属性覆盖；afterEach 删除后恢复原型上的 getter */
function setDocumentHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get: () => hidden,
  });
}

function dispatchVisibilityChange() {
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  // 删除自有的 hidden 覆盖，露出 jsdom 原型上的 getter
  delete (document as unknown as { hidden?: boolean }).hidden;
  vi.useRealTimers();
});

describe('useAutoRefresh', () => {
  it('挂载后不立即触发，每满 60 秒触发一次', () => {
    const onRefresh = vi.fn();
    renderHook(() => useAutoRefresh(onRefresh));

    expect(onRefresh).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS - 1);
    });
    expect(onRefresh).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // 触发后重新计时：再过满一个周期才会第二次触发
    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS);
    });
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it('reset() 后从调用时刻重新计时', () => {
    const onRefresh = vi.fn();
    const { result } = renderHook(() => useAutoRefresh(onRefresh));

    // 先走掉大半个周期，再手动重置
    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS - 1_000);
    });
    act(() => {
      result.current();
    });

    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS - 1);
    });
    expect(onRefresh).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('页面隐藏时不触发；恢复可见后立即触发一次并重新计时', () => {
    const onRefresh = vi.fn();
    setDocumentHidden(true);
    renderHook(() => useAutoRefresh(onRefresh));

    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS * 3);
    });
    expect(onRefresh).not.toHaveBeenCalled();

    setDocumentHidden(false);
    act(() => {
      dispatchVisibilityChange();
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    // 回到前台后重新开始计时
    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS);
    });
    expect(onRefresh).toHaveBeenCalledTimes(2);
  });

  it('运行中切到后台会暂停计时，回到前台立即触发一次', () => {
    const onRefresh = vi.fn();
    renderHook(() => useAutoRefresh(onRefresh));

    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS - 1_000);
    });

    setDocumentHidden(true);
    act(() => {
      dispatchVisibilityChange();
    });
    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS * 2);
    });
    expect(onRefresh).not.toHaveBeenCalled();

    setDocumentHidden(false);
    act(() => {
      dispatchVisibilityChange();
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('卸载后不再触发', () => {
    const onRefresh = vi.fn();
    const { unmount } = renderHook(() => useAutoRefresh(onRefresh));

    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);

    unmount();

    act(() => {
      vi.advanceTimersByTime(PM_AUTO_REFRESH_MS * 3);
    });
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('工具条「自动刷新」说明在 zh / en 均已配置', () => {
    const zh = platformZh.platformMonitor.autoRefresh;
    const en = platformEn.platformMonitor.autoRefresh;

    expect(typeof zh).toBe('string');
    expect(zh.trim()).not.toBe('');
    expect(zh).toContain('60 秒');

    expect(typeof en).toBe('string');
    expect(en.trim()).not.toBe('');
  });

  it('工具条「统计截至」说明在 zh / en 均已配置', () => {
    const zh = platformZh.platformMonitor.statsAsOf;
    const en = platformEn.platformMonitor.statsAsOf;

    expect(typeof zh).toBe('string');
    expect(zh.trim()).not.toBe('');
    expect(zh).toContain('统计截至');
    // 占位符必须对上页面传参的 time，否则渲染出来会是原样花括号
    expect(zh).toContain('{{time}}');

    expect(typeof en).toBe('string');
    expect(en.trim()).not.toBe('');
    expect(en).toContain('{{time}}');
  });
});
