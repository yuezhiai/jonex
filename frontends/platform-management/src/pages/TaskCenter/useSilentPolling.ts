import { useEffect, useRef } from 'react';

/**
 * 按固定节奏重复做一件「用户没要求、但数据得跟上」的取数。
 *
 * 只负责计时，不管取什么、也不管失败 —— 退避靠传进来的 `intervalMs` 变化重启定时器实现，
 * 开关靠 `enabled`（判据由调用方按各自的数据给）。
 */
export default function useSilentPolling(enabled: boolean, reload: () => void, intervalMs: number): void {
  // 回调通常是内联箭头函数。放进依赖会让定时器每渲染一次就重建、计时被不断归零，
  // 间隔形同虚设，所以用 ref 持有最新值。
  const reloadRef = useRef(reload);
  reloadRef.current = reload;

  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(() => {
      // 页面在后台时跳过这一拍：用户看不见，刷了也白刷。
      // 回到前台不必补一次 —— 下一拍（不超过间隔）就追上了。
      if (document.hidden) return;
      reloadRef.current();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, intervalMs]);
}
