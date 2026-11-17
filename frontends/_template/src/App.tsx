import React from 'react';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { withCssVarKey } from '@jonex/platform-theme';
import AppRoute from '@/router';

export default function App() {
  const locale = (localStorage.getItem('locale') || 'zh') === 'zh' ? zhCN : enUS;

  return (
    <ConfigProvider locale={locale} theme={withCssVarKey('jonex-template')}>
      <AppRoute mode="standalone" />
    </ConfigProvider>
  );
}
