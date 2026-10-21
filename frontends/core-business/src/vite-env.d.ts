/// <reference types="vite/client" />

declare module '*.module.scss' {
  const classes: { readonly [key: string]: string };
  export default classes;
}

declare module '*.scss' {
  const content: string;
  export default content;
}

declare module '@loadable/component' {
  import type { ComponentType } from 'react';
  const loadable: (loadFn: () => Promise<{ default: ComponentType<any> }>) => ComponentType<any>;
  export default loadable;
}

declare module 'lodash' {
  export function throttle<T extends (...args: any[]) => any>(
    fn: T,
    wait?: number,
    options?: { leading?: boolean; trailing?: boolean },
  ): T;
}

/** ECharts 把元素上的私有数据（ecData）存在 `makeInner()` 生成的槽里，键名不可预测；
 *  而且 `echarts/core` 并不导出 `getECData`，只能走内部路径。补一份窄声明：
 *  KnowledgeGraphPanel 的搜索聚焦只用到 `focus` 这一个字段，
 *  见该目录 index.tsx 里 `applySearchFocus` 的说明 */
declare module 'echarts/lib/util/innerStore.js' {
  export function getECData(el: unknown): {
    /** 邻接聚焦集合，`blurSeries` 照它决定谁被压暗；非聚焦元素上没有这个字段 */
    focus?: { node: number[]; edge: number[] } | null;
  };
}
