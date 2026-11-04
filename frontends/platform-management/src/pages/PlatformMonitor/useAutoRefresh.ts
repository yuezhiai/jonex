import { useCallback, useEffect, useRef } from 'react';

/** 平台监控页的自动刷新周期：60 秒 */
export const PM_AUTO_REFRESH_MS = 60_000;

/**
 * 停留页面期间按固定周期触发一次刷新回调。
 *
 * 用**可重复武装的 `setTimeout`** 而不是 `setInterval`：每次触发后重新计时，
 * 周期从「上一次触发完成」起算。`setInterval` 的固定节拍在页面卡顿或请求较慢时
 * 会积压回调，多次刷新挤在一起。
 *
 * 页面在后台（`document.hidden`）时**不武装定时器**：后台标签页的刷新结果用户看不到，
 * 白白消耗接口与数据库；浏览器也可能对后台定时器降频，节拍本来就不可信。
 * 回到前台时立即刷新一次，让用户看到的不是离开前那一屏旧数据。
 *
 * @returns `reset`：清掉当前计时并重新开始（手动刷新后调用）
 */
export function useAutoRefresh(onRefresh: () => void, intervalMs: number = PM_AUTO_REFRESH_MS): () => void {
  // 最新回调存 ref：调用方通常传内联箭头函数，effect 不应因此重建定时器。
  // effect 只依赖 intervalMs。
  const callbackRef = useRef(onRefresh);
  useEffect(() => {
    callbackRef.current = onRefresh;
  }, [onRefresh]);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** 当前的「武装」函数。`reset` 在 effect 之外调用，需要拿到最新那份实现 */
  const scheduleRef = useRef<() => void>(() => {});

  useEffect(() => {
    const clearTimer = () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };

    const schedule = () => {
      clearTimer();
      // 隐藏时不武装：等 visibilitychange 回到前台再启动
      if (document.hidden) return;
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        callbackRef.current();
        // 触发后重新武装，形成「一次性定时器串成的循环」
        schedule();
      }, intervalMs);
    };
    scheduleRef.current = schedule;

    const handleVisibilityChange = () => {
      if (document.hidden) {
        clearTimer();
        return;
      }
      // 回到前台：先补一次刷新，再重新计时
      callbackRef.current();
      schedule();
    };

    schedule();
    document.addEventListener('visibilitychange', handleVisibilityChange);

    return () => {
      clearTimer();
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [intervalMs]);

  // 手动刷新后重置周期：不重置的话，刚点完可能立刻又赶上自动刷新的节拍
  return useCallback(() => scheduleRef.current(), []);
}
