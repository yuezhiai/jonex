import React, { useEffect, useState } from 'react';
import { I18nextProvider, useTranslation } from 'react-i18next';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { withCssVarKey } from '@jonex/platform-theme';
import i18n from '@/locales/i18n';
import AppRoute from '@/router';

/** 位于 I18nextProvider 内，响应语言变化更新 Ant Design locale */
function AntdGate({ children }: { children: React.ReactNode }) {
  const { i18n: appI18n } = useTranslation();
  const antdLocale = appI18n.language === 'zh' ? zhCN : enUS;
  // cssVar key 固定为本应用的 app key，避免与 shell 的 cssVar 作用域串号（原因见 withCssVarKey 注释）。
  return (
    <ConfigProvider locale={antdLocale} theme={withCssVarKey('jonex-ecosystem-management')}>
      {children}
    </ConfigProvider>
  );
}

/**
 * 监听来自 Shell 的语言切换事件：
 * - `jonex:locale-change` CustomEvent（Module Federation 同 window 模式）
 * - `message` postMessage（iframe standalone fallback 模式）
 */
function GlobalLocaleListener() {
  const { i18n: appI18n } = useTranslation();
  const [, forceRender] = useState(0);

  useEffect(() => {
    // CustomEvent（同 window）
    const onCustomEvent = (e: CustomEvent<string>) => {
      appI18n.changeLanguage(e.detail);
      forceRender((n) => n + 1);
    };
    // postMessage（iframe 跨域/跨窗口）
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

export default function App() {
  return (
    <I18nextProvider i18n={i18n}>
      <GlobalLocaleListener />
      <AntdGate>
        <AppRoute mode="standalone" />
      </AntdGate>
    </I18nextProvider>
  );
}
