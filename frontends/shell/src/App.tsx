import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { BrowserRouter, Routes, Route, Navigate, Outlet, useLocation } from 'react-router-dom';
import { ConfigProvider, Spin } from 'antd';
import { StyleProvider } from '@ant-design/cssinjs';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { LANGUAGE_STORAGE_KEY } from '@jonex/i18n-resources';
import { antdTheme, sharedCache } from '@jonex/platform-theme';
import Login from './pages/Login';
import AppShellLayout from './components/AppShellLayout';
import Dashboard from './pages/Dashboard';
import AppHost from './pages/AppHost';
import { getAccessToken } from './api/auth';
import type { ReactNode } from 'react';

function RequireAuth({ children }: { children: ReactNode }) {
  const [authChecked, setAuthChecked] = useState(false);

  // token 过期（jonex:token-expired 事件 / 轮询兜底 / 路由守卫）只处理一次：
  // 页面并发请求同时 401 时会收到多个事件，全部挡掉只跳转一次。
  // 「会话已过期」提示统一在登录页展示（?expired=1），这里不重复提示。
  const redirectingRef = useRef(false);
  const goLogin = useCallback(() => {
    if (redirectingRef.current) return;
    redirectingRef.current = true;
    window.location.href = `/login?redirect=${encodeURIComponent(window.location.href)}&expired=1`;
  }, []);

  useEffect(() => {
    if (getAccessToken()) {
      setAuthChecked(true);
    } else {
      goLogin();
    }
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
      <ConfigProvider locale={antdLocale} theme={{ ...antdTheme }}>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
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
