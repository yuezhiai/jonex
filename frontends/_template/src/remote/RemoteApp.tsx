import React from 'react';
import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { withCssVarKey } from '@jonex/platform-theme';
import '@/locales/i18n';
import AppRoute from '@/router';
import { writeAccessToken, writeCachedUser } from '@jonex/shell-sdk';

interface ShellContext {
  basePath?: string;
  token?: string;
  user?: Record<string, unknown>;
  locale?: string;
}

export default function mount(container: HTMLElement, shellContext?: ShellContext): () => void {
  const { basePath, token, user, locale: shellLocale } = shellContext || {};

  if (token) {
    writeAccessToken(token);
  }
  if (user) {
    writeCachedUser(user);
  }

  const antdLocale = shellLocale === 'en' ? enUS : zhCN;
  const root = createRoot(container);

  root.render(
    <ConfigProvider locale={antdLocale} theme={withCssVarKey('jonex-template')}>
      <AppRoute basename={basePath || '/apps/{{APP_ID}}'} mode="hosted" shellContext={shellContext} />
    </ConfigProvider>,
  );

  let mounted = true;
  return () => {
    if (!mounted) return;
    mounted = false;
    root.unmount();
  };
}
