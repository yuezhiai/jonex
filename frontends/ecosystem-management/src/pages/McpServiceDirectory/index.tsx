import { useTranslation } from 'react-i18next';
import { Card, Tabs } from 'antd';
import { AppstoreOutlined, KeyOutlined } from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import McpServicesTab from './McpServicesTab';
import McpKeysTab from './McpKeysTab';

/**
 * MCP 服务管理 — 页面外壳
 *
 * 两个一级 Tab（URL ?tab 参数驱动，hosted MemoryRouter 下亦生效）：
 * - tab=catalog（默认）：服务目录（只读）
 * - tab=keys：Key 管理（统一 Key：服务授权 + 知识写入）
 */
export default function McpServiceDirectory() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get('tab') === 'keys' ? 'keys' : 'catalog';

  /** Tab 切换同步 URL ?tab 参数（replace，保留其他 query） */
  const handleTabChange = (key: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (key === 'keys') next.set('tab', 'keys');
        else next.delete('tab');
        return next;
      },
      { replace: true },
    );
  };

  return (
    <>
      <PageHeader title={t('mcpServiceDirectory.title')} />

      <PageContent showCard={false}>
        <Card className="mcp-panel" styles={{ body: { padding: 0 } }}>
          <Tabs
            className="mcp-tabs"
            activeKey={activeTab}
            onChange={handleTabChange}
            items={[
              {
                key: 'catalog',
                label: (
                  <span className="mcp-tab-label">
                    <AppstoreOutlined />
                    {t('mcpServiceDirectory.tabCatalog')}
                  </span>
                ),
                children: <McpServicesTab onGoToKeys={() => handleTabChange('keys')} />,
              },
              {
                key: 'keys',
                label: (
                  <span className="mcp-tab-label">
                    <KeyOutlined />
                    {t('mcpServiceDirectory.tabKeys')}
                  </span>
                ),
                children: <McpKeysTab />,
              },
            ]}
          />
        </Card>
      </PageContent>
    </>
  );
}
