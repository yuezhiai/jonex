import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider, useTranslation } from 'react-i18next';
import { ConfigProvider } from 'antd';
import { StyleProvider } from '@ant-design/cssinjs';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { antdTheme, sharedCache } from '@jonex/platform-theme';
import i18n from '@/locales/i18n';
import '@/styles/index.scss';
import '@jonex/platform-theme/theme.css';
import '@jonex/platform-theme/layout.css';
import AppRoute from '@/router';
import { writeAccessToken, writeCachedUser } from '@jonex/shell-sdk';

interface ShellContext {
  basePath?: string;
  token?: string;
  user?: Record<string, unknown>;
  locale?: string;
}

/**
 * 位于 I18nextProvider 内，实时响应语言切换更新 Ant Design locale。
 */
function AntdLocaleGate({ children }: { children: React.ReactNode }) {
  const { i18n: appI18n } = useTranslation();
  const antdLocale = appI18n.language === 'en' ? enUS : zhCN;
  // cssVar 关闭：见 App.tsx 说明。避免懒加载切换后 popup z-index 变量丢失导致弹层被盖住。
  return (
    <StyleProvider cache={sharedCache}>
      <ConfigProvider locale={antdLocale} theme={{ ...antdTheme }}>
        {children}
      </ConfigProvider>
    </StyleProvider>
  );
}

/**
 * 监听来自 Shell 的语言切换事件：
 * - `jonex:locale-change` CustomEvent（Module Federation 同 window 模式）
 * - `message` postMessage（iframe standalone fallback 模式）
 */
function LocaleController() {
  const { i18n: appI18n } = useTranslation();
  const [, forceRender] = useState(0);

  useEffect(() => {
    const onCustomEvent = (e: CustomEvent<string>) => {
      appI18n.changeLanguage(e.detail);
      forceRender((n) => n + 1);
    };
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type === 'jonex:locale-change') {
        appI18n.changeLanguage(e.data.locale);
        forceRender((n) => n + 1);
      }
    };
    window.addEventListener('jonex:locale-change', onCustomEvent as EventListener);
    window.addEventListener('message', onMessage);
    return () => {
      window.removeEventListener('jonex:locale-change', onCustomEvent as EventListener);
      window.removeEventListener('message', onMessage);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return null;
}

function HostedApp({ shellContext }: { shellContext: ShellContext }) {
  const { basePath, locale: shellLocale } = shellContext;

  // 初始同步
  useEffect(() => {
    if (shellLocale && shellLocale !== i18n.language) {
      i18n.changeLanguage(shellLocale);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <I18nextProvider i18n={i18n}>
      <LocaleController />
      <AntdLocaleGate>
        <AppRoute basename={basePath || '/apps/core-business'} mode="hosted" shellContext={shellContext} />
      </AntdLocaleGate>
    </I18nextProvider>
  );
}

export default function mount(container: HTMLElement, shellContext?: ShellContext): () => void {
  const { token, user } = shellContext || {};

  // 标记 Shell 托管模式，阻止 main.tsx 中的 startStandalone()
  (window as any).__SHELL_CONTEXT__ = shellContext || {};

  if (token) {
    writeAccessToken(token);
  }
  if (user) {
    writeCachedUser(user);
  }

  const root = createRoot(container);

  root.render(<HostedApp shellContext={shellContext || {}} />);

  let mounted = true;
  return () => {
    if (!mounted) return;
    mounted = false;
    root.unmount();
  };
}
