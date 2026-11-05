/**
 * vitest 全局 setup —— **所有**测试文件都会加载它（`*.test.ts` 跑 node 环境也会执行到
 * 这里），所以每段 polyfill 都必须先过 `typeof window !== 'undefined'` 守卫：node 环境
 * 连 `window` 都不存在，裸写会在既有纯逻辑测试里直接抛 ReferenceError。
 *
 * 只放 jsdom 下组件**确证需要**的：antd 的响应式断点 / 容器观测在 jsdom 里没有原生
 * 实现（`matchMedia`、`ResizeObserver`、`IntersectionObserver`），缺了会在组件挂载时
 * 直接报 "not a function"。
 */

if (typeof window !== 'undefined') {
  // antd 响应式（断点 hooks / 响应式 Modal）读 matchMedia；jsdom 不实现它。
  if (typeof window.matchMedia !== 'function') {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      configurable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        // 旧 API（antd 部分路径仍在用 addListener/removeListener）
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    });
  }

  // rc-resize-observer（Modal body 滚动、Typography 等）依赖 ResizeObserver。
  if (typeof (window as any).ResizeObserver !== 'function') {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    (window as any).ResizeObserver = ResizeObserverStub;
    (globalThis as any).ResizeObserver = ResizeObserverStub;
  }

  // rc-virtual-list / 折叠动画等用 IntersectionObserver 做可见性观测。
  if (typeof (window as any).IntersectionObserver !== 'function') {
    class IntersectionObserverStub {
      root = null;
      rootMargin = '';
      thresholds: number[] = [];
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
    }
    (window as any).IntersectionObserver = IntersectionObserverStub;
    (globalThis as any).IntersectionObserver = IntersectionObserverStub;
  }
}
