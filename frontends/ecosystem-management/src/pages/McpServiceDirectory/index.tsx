import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, Tabs } from 'antd';
import { colors, radius } from '@jonex/platform-theme/tokens';
import McpServicesTab from './McpServicesTab';
import McpKeysTab from './McpKeysTab';

/**
 * MCP 服务目录 — 页面外壳
 *
 * 描述下方以 Tab 聚合两个列表能力：
 * - MCP 服务：发布 / 取消发布 / 测试调用 / 授权 Key
 * - MCP Key：原 MCP Key 管理页面的列表能力（含 WorkBuddy 引导）
 */
export default function McpServiceDirectory() {
  const { t } = useTranslation();
  // 支持从 URL 参数定位到指定 Tab（如 ?tab=keys 直接定位到 MCP Key）
  const [activeTab, setActiveTab] = useState<string>(() => {
    if (typeof window !== 'undefined') {
      const params = new URLSearchParams(window.location.search);
      if (params.get('tab') === 'keys') return 'keys';
    }
    return 'services';
  });

  return (
    <div>
      <div className="yx-page-title">
        <h1 style={{ fontSize: 24, fontWeight: 700, color: colors.brandDark, marginBottom: 4 }}>
          {t('mcpServiceDirectory.title')}
        </h1>
        <p style={{ color: colors.textMuted, margin: '4px 0 0', fontSize: 14 }}>
          {t('mcpServiceDirectory.pageSubtitle')}
        </p>
      </div>

      <Card style={{ borderRadius: radius.card }}>
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: 'services',
              label: t('mcpServiceDirectory.tabServices'),
              children: (
                <div>
                  <div className="mcp-svc-tab-head">
                    <h2>{t('mcpServiceDirectory.tabServicesTitle')}</h2>
                    <p>{t('mcpServiceDirectory.tabServicesDesc')}</p>
                  </div>
                  <McpServicesTab />
                </div>
              ),
            },
            {
              key: 'keys',
              label: t('mcpServiceDirectory.tabKeys'),
              children: (
                <div>
                  <div className="mcp-svc-tab-head">
                    <h2>{t('mcpServiceDirectory.tabKeysTitle')}</h2>
                    <p>{t('mcpServiceDirectory.tabKeysDesc')}</p>
                  </div>
                  <McpKeysTab />
                </div>
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
