import { useTranslation } from 'react-i18next';
import { Segmented } from 'antd';
import { AppstoreOutlined } from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import McpServicesTab from './McpServicesTab';
import McpKeysTab from './McpKeysTab';
import './index.css';

/**
 * MCP 领域服务 Tab — 卡片头（标题 + 描述 + 视图切换）
 *
 * 视图切换（URL ?view 参数驱动，hosted MemoryRouter 下亦生效）：
 * - view=services（默认）：服务目录（McpServicesTab）
 * - view=access：服务访问Key（McpKeysTab）
 * 兼容旧参数 ?view=keys。
 */
export default function McpDomainServicesTab() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const view: 'services' | 'keys' =
    searchParams.get('view') === 'access' || searchParams.get('view') === 'keys' ? 'keys' : 'services';

  /** 视图切换同步 URL ?view（replace，写 access 约定值，保留其他 query） */
  const handleViewChange = (v: string | number) => {
    const next = v === 'keys' ? 'keys' : 'services';
    setSearchParams(
      (prev) => {
        const p = new URLSearchParams(prev);
        if (next === 'keys') p.set('view', 'access');
        else p.delete('view');
        return p;
      },
      { replace: true },
    );
  };

  return (
    <div>
      <div className="mcp-svc-card-head">
        <div className="mcp-svc-card-head-inner">
          <AppstoreOutlined className="mcp-svc-card-icon" />
          <div>
            <h2>{t('mcpServiceDirectory.domainCardTitle')}</h2>
            <p>{t('mcpServiceDirectory.domainCardDesc')}</p>
          </div>
        </div>
        <Segmented
          value={view}
          onChange={handleViewChange}
          options={[
            { value: 'services', label: t('mcpServiceDirectory.viewDirectory') },
            { value: 'keys', label: t('mcpServiceDirectory.viewAccess') },
          ]}
        />
      </div>

      {view === 'services' ? (
        <McpServicesTab onGoToKeys={() => handleViewChange('keys')} />
      ) : (
        <McpKeysTab />
      )}
    </div>
  );
}
