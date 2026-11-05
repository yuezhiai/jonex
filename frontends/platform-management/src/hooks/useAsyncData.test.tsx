// @vitest-environment jsdom
/**
 * `useAsyncData` 的静默重取。
 *
 * 行为约定：
 *  - 首次取数与「取数条件变了」（load 引用变化）都是可见的：loading 置 true；
 *  - 只有 `silentKey` 变化时静默：不动 loading、失败保留上一份数据、成功后撤掉旧错误。
 */
import { StrictMode } from 'react';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import useAsyncData from './useAsyncData';

afterEach(cleanup);

/** 把已 resolve / reject 的微任务跑完：hook 的 then/catch 在下一轮才落状态 */
async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('useAsyncData', () => {
  it('首次取数是要看得见的 loading', async () => {
    const load = () => Promise.resolve('first');
    const seen: boolean[] = [];
    const { result } = renderHook(() => {
      const state = useAsyncData(load);
      seen.push(state.loading);
      return state;
    });

    expect(seen[0]).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBe('first');
  });

  it('只有 silentKey 变化时才静默：loading 全程保持 false', async () => {
    let calls = 0;
    const load = () => Promise.resolve(++calls);
    const seen: boolean[] = [];
    const { result, rerender } = renderHook(
      ({ tick }: { tick: number }) => {
        const state = useAsyncData(load, { silentKey: tick });
        seen.push(state.loading);
        return state;
      },
      { initialProps: { tick: 0 } },
    );

    await waitFor(() => expect(result.current.data).toBe(1));
    seen.length = 0;

    rerender({ tick: 1 });
    await waitFor(() => expect(result.current.data).toBe(2));
    await flush();

    expect(seen.every((loading) => loading === false)).toBe(true);
  });

  it('静默重取失败时保留上一份数据，也不进错误态', async () => {
    let calls = 0;
    const load = () => (++calls === 1 ? Promise.resolve('kept') : Promise.reject(new Error('boom')));
    const { result, rerender } = renderHook(
      ({ tick }: { tick: number }) => useAsyncData(load, { silentKey: tick }),
      { initialProps: { tick: 0 } },
    );

    await waitFor(() => expect(result.current.data).toBe('kept'));

    rerender({ tick: 1 });
    await flush();

    expect(result.current.data).toBe('kept');
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('静默重取成功后撤掉上一轮的失败提示', async () => {
    let calls = 0;
    const load = () => (++calls === 1 ? Promise.reject(new Error('boom')) : Promise.resolve('recovered'));
    const { result, rerender } = renderHook(
      ({ tick }: { tick: number }) => useAsyncData(load, { silentKey: tick }),
      { initialProps: { tick: 0 } },
    );

    await waitFor(() => expect(result.current.error).toBeTruthy());

    rerender({ tick: 1 });
    await waitFor(() => expect(result.current.data).toBe('recovered'));
    expect(result.current.error).toBeNull();
  });

  it('StrictMode 下首屏仍是可见 loading：重跑 effect 不因来源相同被当成静默', async () => {
    const load = () => Promise.resolve('first');
    const states: { loading: boolean; hasData: boolean }[] = [];
    const { result } = renderHook(
      () => {
        const state = useAsyncData(load);
        states.push({ loading: state.loading, hasData: state.data !== null });
        return state;
      },
      { wrapper: StrictMode },
    );

    await waitFor(() => expect(result.current.data).toBe('first'));

    // 「还没取到数、却已经不在 loading」= 首屏那一下骨架屏被提前撤掉了
    expect(states.some((state) => !state.loading && !state.hasData)).toBe(false);
  });

  it('取数条件变了（load 引用变化）走可见路径，不被当成静默', async () => {
    const loadA = () => Promise.resolve('a');
    const loadB = () => Promise.resolve('b');
    const seen: boolean[] = [];
    const { result, rerender } = renderHook(
      ({ load }: { load: () => Promise<string> }) => {
        const state = useAsyncData(load, { silentKey: 0 });
        seen.push(state.loading);
        return state;
      },
      { initialProps: { load: loadA } },
    );

    await waitFor(() => expect(result.current.data).toBe('a'));
    seen.length = 0;

    rerender({ load: loadB });
    await waitFor(() => expect(result.current.data).toBe('b'));

    // loading 是在 effect 里置起的：rerender 当次那帧还是旧值，重渲染后亮过即可
    expect(seen).toContain(true);
  });
});
