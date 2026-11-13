import { useEffect, useState } from 'react';

/**
 * 表体高度宿主：把量到的容器高度作为 antd `scroll.y` 的上限。
 *
 * 为什么量容器而不是写 `calc(100vh - N)`：hosted 模式页面跑在 shell 的 iframe 里，
 * `100vh` 是 iframe 的高度，比 standalone 少一个顶栏，同一个 N 会一边留白一边溢出
 * （platform-management 的 CallRecords 同一处理）。
 *
 * 这个高度只是 `max-height` 的**上限**，真实高度由 platform-theme 的 `.yx-table-fill`
 * flex 链分配，所以取容器全高即可，不必自己扣表头与分页的高度。
 *
 * 用法：把返回的 ref 挂到表格外的宿主 div 上（该 div 必须是 flex 子项、有确定高度），
 * 表格加 `className="yx-table-fill"`，并把返回的 height 透传给 `scroll.y`。
 */
export function useFilledTableHeight(minHeight = 200) {
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const [height, setHeight] = useState(minHeight);

  useEffect(() => {
    if (!host) return undefined;
    const sync = () => setHeight(Math.max(host.clientHeight, minHeight));
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(host);
    return () => observer.disconnect();
  }, [host, minHeight]);

  // 节点存 state 而不是 useRef：错误态重试、Tab 切换都会把宿主 div 卸载再挂回来，
  // ref 对象换节点不会触发副作用，量到的高度会一直停在上一次的值（下限 200）。
  return [setHost, height] as const;
}
