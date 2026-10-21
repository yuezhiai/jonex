/**
 * 子应用路由 → 顶层地址栏的同步协议。
 *
 * 地址栏归顶层窗口：hosted（Module Federation）下父子同窗，iframe fallback 下地址栏在父窗，
 * 子应用只能自己去写。但写完 shell 是不知道的 —— 它既没被派发 popstate（浏览器对
 * 「只换了 iframe 文档」的历史遍历只在子文档里触发），也没经过自己的 router。
 * 于是 shell 会一直停在自己上一次导航的位置：侧边栏高亮错位，且再点同一个跳转时因为
 * URL 没变化、算出来的 iframe src 与上一次相同，iframe 根本不会被驱动。
 *
 * 所以每次写地址栏都补一条通知，由 shell 决定要不要跟进（见 shell 的 CrossAppNavigator）。
 */
import { isEmbedded } from './embed';
import type { ShellContext } from './types';

/** 路由同步事件名（同窗事件与 postMessage 共用）。与 jonex:navigate 同属窗口级约定。 */
export const SHELL_ROUTE_SYNC_EVENT = 'jonex:route-sync';

export interface ShellRouteSyncDetail {
  /** 顶层地址栏的完整路径，形如 `/apps/<appId>/...` */
  path: string;
}

/**
 * 把子应用当前路由写进顶层地址栏并通知 shell。
 *
 * @param appId 子应用 id，用于拼 `/apps/<appId>` 前缀
 * @param routePath 子应用内部路由，形如 `/task-center?x=1`
 */
export function syncShellRoute(appId: string, routePath: string): void {
  if (typeof window === 'undefined') return;
  // 根路由不代表任何业务页面，写进去只会把地址栏擦成应用首页
  if (routePath === '/' || routePath === '/home') return;

  const path = `${resolveShellBasePath(appId)}${routePath}`;
  const top = window.parent !== window ? window.parent : window;
  try {
    top.history.replaceState({}, '', path);
  } catch {
    // 跨域父窗读不到 history：写本窗，至少本页地址不漂
    window.history.replaceState({}, '', path);
  }

  notifyShellRoute(path);
}

/** 子应用在 shell 里的挂载前缀：被嵌/hosted 时带 `/apps/`，独立端口直接开时没有。 */
export function resolveShellBasePath(appId: string): string {
  const inIframe = window.parent !== window;
  const win = (inIframe ? window.parent : window) as Window & { __SHELL_CONTEXT__?: ShellContext };
  const shellCtx = win.__SHELL_CONTEXT__;

  let basePath: string;
  if (inIframe) {
    basePath = `/apps/${appId}`;
  } else if (shellCtx?.basePath) {
    basePath = shellCtx.basePath;
  } else if (shellCtx) {
    basePath = `/apps/${appId}`;
  } else {
    basePath = `/${appId}`;
  }
  return basePath.endsWith('/') ? basePath.slice(0, -1) : basePath;
}

function notifyShellRoute(path: string): void {
  if (isEmbedded() && window.parent !== window) {
    window.parent.postMessage({ type: SHELL_ROUTE_SYNC_EVENT, path }, window.location.origin);
    return;
  }
  window.dispatchEvent(
    new CustomEvent<ShellRouteSyncDetail>(SHELL_ROUTE_SYNC_EVENT, { detail: { path } }),
  );
}
