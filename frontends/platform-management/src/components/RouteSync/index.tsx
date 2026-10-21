import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { syncShellRoute } from '@jonex/shell-sdk';

const APP_ID = 'platform-management';

/**
 * 将子应用的内部路由同步到浏览器地址栏（并通知 shell 跟上）。
 * 嵌入模式（iframe）下以父窗口 /apps/ 路径为准。
 */
export default function RouteSync() {
  const location = useLocation();
  const path = location.pathname + location.search + location.hash;

  useEffect(() => {
    syncShellRoute(APP_ID, path);
    // bfcache 还原不会重跑 effect：地址栏与 shell 都还停在离开时的位置，得补一次
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) syncShellRoute(APP_ID, path);
    };
    window.addEventListener('pageshow', onPageShow);
    return () => window.removeEventListener('pageshow', onPageShow);
  }, [path]);

  return null;
}
