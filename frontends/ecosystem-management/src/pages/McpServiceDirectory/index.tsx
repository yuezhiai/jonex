import { useTranslation } from 'react-i18next';
import { Card, Tabs } from 'antd';
import { useSearchParams } from 'react-router-dom';
import { colors, radius } from '@jonex/platform-theme/tokens';
import McpDomainServicesTab from './McpDomainServicesTab';
import McpWriteKeysTab from './McpWriteKeysTab';

/**
 * MCP 服务管理 — 页面外壳
 *
 * 两个 Tab（URL ?area 参数驱动，hosted MemoryRouter 下亦生效）：
 * - area=domain（默认）：MCP领域服务（卡片头：服务目录 / 服务访问Key 视图切换）
 * - area=write：MCP知识写入（知识写入 Key 管理，Phase 17 WRITE-01/02/03）
 */
export default function McpServiceDirectory() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = searchParams.get('area') === 'write' ? 'write' : 'domain';

  /** Tab 切换同步 URL ?area 参数（replace，保留其他 query 如 view） */
  const handleTabChange = (key: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (key === 'write') next.set('area', 'write');
        else next.delete('area');
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
              key: 'domain',
              label: t('mcpServiceDirectory.tabDomainServices'),
              children: <McpDomainServicesTab />,
            },
            {
              key: 'write',
              label: t('mcpServiceDirectory.tabWrite'),
              children: <McpWriteKeysTab />,
            },
          ]}
        />
      </Card>
    </div>
  );
}
