import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { BrowserRouter, Routes, Route, Navigate, Outlet, useLocation } from 'react-router-dom';
import { ConfigProvider, Spin } from 'antd';
import { StyleProvider } from '@ant-design/cssinjs';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { LANGUAGE_STORAGE_KEY } from '@jonex/i18n-resources';
import { sharedCache, withCssVarKey } from '@jonex/platform-theme';
import { redirectToLogin, writeCachedUser, scheduleAutoRefresh, refreshTokens } from '@jonex/shell-sdk';
import Login from './pages/Login';
import SsoError from './pages/SsoError';
import AppShellLayout from './components/AppShellLayout';
import CrossAppNavigator from './components/CrossAppNavigator';
import Dashboard from './pages/Dashboard';
import AppHost from './pages/AppHost';
import { getAccessToken, fetchCurrentUser, isAccessTokenExpired } from './api/auth';
import { clearSsoNavigationInFlight } from './utils/ssoInflight';
import type { ShellUser } from '@jonex/shell-sdk';
import type { ReactNode } from 'react';

function RequireAuth({ children }: { children: ReactNode }) {
  const [authChecked, setAuthChecked] = useState(false);

  // token 过期（jonex:token-expired 事件 / 轮询兜底 / 路由守卫）只处理一次：
  // 页面并发请求同时 401 时会收到多个事件，全部挡掉只跳转一次。
  // 「会话已过期」提示统一在登录页展示（?expired=1），这里不重复提示。
  const redirectingRef = useRef(false);
  const goLogin = useCallback(() => {
    if (redirectingRef.current) return;
    // 主动登出的顶层导航在途时让位，且**不上锁**：若该导航被浏览器取消，
    // 兜底窗口过后下一轮轮询仍能收尾，避免用户永久停在没有登录态的页面上。
    if (!redirectToLogin({ loginUrl: '/login', expired: true })) return;
    redirectingRef.current = true;
  }, []);

  useEffect(() => {
    let cancelled = false;
    const proceed = (user: ShellUser | null) => {
      if (cancelled) return;
      if (user) writeCachedUser(user);
      setAuthChecked(true);
      // 刷新页面 / 重新打开时已有有效 token：重启定时自动刷新（滑动续期）。
      scheduleAutoRefresh();
    };

    // SSO 登录后 access 为空（后端只下发 httpOnly refresh cookie、不下发 access）：
    // 不能直接跳登录，先尝试走 cookie refresh 换 access。后端 /auth/refresh 端点
    // 无 cookie 时快速 401（RefreshTokenRevokedError），失败才跳登录。
    if (!getAccessToken()) {
      refreshTokens().then((ok) => {
        if (cancelled) return;
        if (ok) {
          // 认证成功即证明那次 SSO 顶层导航已经完成，清掉跨文档哨兵：哨兵当初无论是否写入，
          // 留着都只会在 15 分钟内的任何无关路径（会话失效跳 /login?expired=1 等）上误报
          // 「企业登录未完成」。清在这里而非 proceed 里：若 /auth/me 随后 401 被拦截器踢回
          // 登录页，登录页也不会与「会话已过期」提示并列弹出第二条互相矛盾的中性提示。
          clearSsoNavigationInFlight();
          // 刷新成功：/auth/me 权威校验兜底，确认用户/权限仍有效
          fetchCurrentUser()
            .then((user) => proceed(user))
            .catch(() => {
              if (cancelled) return;
              if (getAccessToken()) setAuthChecked(true);
            });
        } else {
          goLogin();
        }
      });
      return () => {
        cancelled = true;
      };
    }

    // 本地同步预判 access token 是否已过期：过期时先用 refresh token 尝试刷新
    // （滑动续期，零额外延迟），刷新成功则继续 /auth/me 权威校验；
    // 刷新失败（refresh 也失效/无 refresh）才跳转登录。未过期则直接走 /auth/me。

    if (isAccessTokenExpired()) {
      refreshTokens().then((ok) => {
        if (cancelled) return;
        if (ok) {
          // 认证成功即证明那次 SSO 顶层导航已经完成，清掉跨文档哨兵：哨兵当初无论是否写入，
          // 留着都只会在 15 分钟内的任何无关路径（会话失效跳 /login?expired=1 等）上误报
          // 「企业登录未完成」。清在这里而非 proceed 里：若 /auth/me 随后 401 被拦截器踢回
          // 登录页，登录页也不会与「会话已过期」提示并列弹出第二条互相矛盾的中性提示。
          clearSsoNavigationInFlight();
          // 刷新成功：/auth/me 权威校验兜底，确认用户/权限仍有效
          fetchCurrentUser()
            .then((user) => proceed(user))
            .catch(() => {
              if (cancelled) return;
              if (getAccessToken()) setAuthChecked(true);
            });
        } else {
          goLogin();
        }
      });
      return () => {
        cancelled = true;
      };
    }

    // /auth/me 权威校验：
    // - 成功 → 先刷新缓存用户再放行（保证 AppShellLayout 挂载时读到最新用户）；
    // - 401/403 → 拦截器已 emitSessionExpired（清 token + 发 jonex:token-expired），
    //   由下方事件监听 goLogin 收尾，这里不 setAuthChecked；
    // - 网络/服务异常（非 401）→ token 仍在，放行，避免离线被误踢。
    // 已有未过期 access token 直接进入权威校验：这同样是一条「进入已登录态」的路径
    // （另一标签页已登录、租户切换/退出模拟的整页重载都落在这里），同样要清 SSO 哨兵，
    // 否则 15 分钟内的会话失效跳转会在登录页弹出自相矛盾的「企业登录未完成」。
    clearSsoNavigationInFlight();
    fetchCurrentUser()
      .then((user) => proceed(user))
      .catch(() => {
        if (cancelled) return;
        if (getAccessToken()) {
          setAuthChecked(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [goLogin]);

  // 监听子应用发来的 token 过期事件
  useEffect(() => {
    const handler = () => goLogin();
    window.addEventListener('jonex:token-expired', handler);
    return () => window.removeEventListener('jonex:token-expired', handler);
  }, [goLogin]);

  // 切换目录时同步检查 token 是否还在
  const location = useLocation();
  useEffect(() => {
    if (authChecked && !getAccessToken()) {
      goLogin();
    }
  }, [location.pathname, authChecked, goLogin]);

  // 轮询兜底：检测到 token 被清除则跳转
  useEffect(() => {
    const timer = setInterval(() => {
      if (!getAccessToken()) goLogin();
    }, 1500);
    return () => clearInterval(timer);
  }, [goLogin]);

  if (!authChecked) {
    return (
      <div
        style={{
          height: '100vh',
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          background: '#f0f4f8',
        }}
      >
        <Spin size="large" />
      </div>
    );
  }
  return <>{children}</>;
}

function AuthenticatedLayout() {
  return (
    <RequireAuth>
      <AppShellLayout>
        <Outlet />
      </AppShellLayout>
    </RequireAuth>
  );
}

function App() {
  const { i18n } = useTranslation();

  // 初始化存储的 locale（首次访问无值时 normalizeLocale 回退到 en）
  const stored = typeof window !== 'undefined' ? window.localStorage.getItem(LANGUAGE_STORAGE_KEY) : null;
  if (stored === null) {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, i18n.language);
  }

  const antdLocale = i18n.language === 'zh' ? zhCN : enUS;

  // 浏览器标签标题随语言切换（直接监听 languageChanged 事件，不依赖 React 重渲染）
  useEffect(() => {
    const update = () => {
      document.title = i18n.t('site.title');
    };
    update();
    i18n.on('languageChanged', update);
    return () => {
      i18n.off('languageChanged', update);
    };
  }, [i18n]);

  return (
    // StyleProvider 统一共享 cssinjs cache（跨所有应用同一 instanceId）：
    // 避免 MF 切换子项目时组件 token 变量 <style>（.antd.ant-dropdown-css-var）被误删
    <StyleProvider cache={sharedCache}>
      <ConfigProvider locale={antdLocale} theme={withCssVarKey('jonex-shell')}>
        <BrowserRouter>
          {/* 子应用之间的跳转（jonex:navigate）：交给本 router 做 SPA 导航，不重载文档 */}
          <CrossAppNavigator />
          <Routes>
            <Route path="/login" element={<Login />} />
            {/* SSO「无访问权限」错误页：由后端 /oidc/callback 302 过来，用户此刻没有登录态，
                必须留在 AuthenticatedLayout 之外 —— 放进去会被 RequireAuth 立刻踢回 /login。 */}
            <Route path="/sso-error" element={<SsoError />} />
            <Route element={<AuthenticatedLayout />}>
              <Route index element={<Dashboard />} />
              <Route path="apps/:appId/*" element={<AppHost />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </ConfigProvider>
    </StyleProvider>
  );
}

export default App;
