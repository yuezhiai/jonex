import { useTranslation } from 'react-i18next';
import { Card, Tabs } from 'antd';
import { useSearchParams } from 'react-router-dom';
import { colors, radius } from '@jonex/platform-theme/tokens';
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
          onChange={handleTabChange}
          items={[
            {
              key: 'catalog',
              label: t('mcpServiceDirectory.tabCatalog'),
              children: <McpServicesTab onGoToKeys={() => handleTabChange('keys')} />,
            },
            {
              key: 'keys',
              label: t('mcpServiceDirectory.tabKeys'),
              children: <McpKeysTab />,
            },
          ]}
        />
      </Card>
    </div>
  );
}
