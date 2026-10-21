import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  CROSS_APP_NAVIGATE_EVENT,
  SHELL_ROUTE_SYNC_EVENT,
  type CrossAppNavigateDetail,
  type ShellRouteSyncDetail,
} from '@jonex/shell-sdk';
import { shouldAdoptRoute } from './routeAdoption';

/**
 * 跨应用导航桥：接住子应用发来的跳转请求，用 SPA 路由跳过去，外层文档不重载。
 *
 * 必须挂在 BrowserRouter 之内（useNavigate 的前提）。两条通道对应 shell 的两种挂载形态：
 * - 同窗事件（hosted / Module Federation）：同步派发，接管时当场 acknowledge，调用方据此
 *   判断「有没有人接」，接不上的会自己回落到整页导航；
 * - postMessage（iframe fallback）：子应用在另一份文档里，事件过不来。
 *
 * 同时负责把 router 拉回地址栏的真实位置：子应用会直接改写顶层地址栏（见 shell-sdk 的
 * syncShellRoute），而浏览器在「只换了 iframe 文档」的历史遍历里不向父文档派 popstate，
 * router 自己永远发现不了地址已经变了 —— 不拉回来，退回到某个应用后 shell 会一直以为
 * 自己还在另一个应用里。
 */
export default function CrossAppNavigator() {
  const navigate = useNavigate();
  const location = useLocation();
  const currentPath = `${location.pathname}${location.search}${location.hash}`;

  useEffect(() => {
    // 只接管跨应用跳转：/apps/<appId>/... 之外的路径（登录页、首页）不属于本协议，
    // 不 acknowledge，交回调用方整页导航
    const go = (path: unknown): boolean => {
      if (typeof path !== 'string' || !path.startsWith('/apps/')) return false;
      navigate(path);
      return true;
    };

    // replace 而不是 push：路由是地址栏先变、这里只是跟上，不该再压一条历史
    const adopt = (path: unknown) => {
      if (!shouldAdoptRoute(currentPath, path)) return;
      navigate(path, { replace: true });
    };

    const onEvent = (event: Event) => {
      const detail = (event as CustomEvent<CrossAppNavigateDetail>).detail;
      if (detail?.path && go(detail.path)) detail.acknowledge();
    };

    const onRouteSyncEvent = (event: Event) => {
      adopt((event as CustomEvent<ShellRouteSyncDetail>).detail?.path);
    };

    const onMessage = (event: MessageEvent) => {
      // 同源校验：iframe fallback 下子应用与 shell 同源，其他来源的 message 一律不认
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; path?: string } | null;
      if (data?.type === SHELL_ROUTE_SYNC_EVENT) {
        adopt(data.path);
        return;
      }
      if (data?.type !== CROSS_APP_NAVIGATE_EVENT) return;
      go(data.path);
    };

    window.addEventListener(CROSS_APP_NAVIGATE_EVENT, onEvent);
    window.addEventListener(SHELL_ROUTE_SYNC_EVENT, onRouteSyncEvent);
    window.addEventListener('message', onMessage);
    return () => {
      window.removeEventListener(CROSS_APP_NAVIGATE_EVENT, onEvent);
      window.removeEventListener(SHELL_ROUTE_SYNC_EVENT, onRouteSyncEvent);
      window.removeEventListener('message', onMessage);
    };
  }, [currentPath, navigate]);

  return null;
}
