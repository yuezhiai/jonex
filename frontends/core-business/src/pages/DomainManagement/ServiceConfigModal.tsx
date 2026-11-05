import { useState } from 'react';
import { Modal, Button, Table, Tabs, Tag, Typography, Empty } from 'antd';
import {
  ApiOutlined,
  PlusOutlined,
  CopyOutlined,
  CheckOutlined,
  DeleteOutlined,
  KeyOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import copy from 'copy-to-clipboard';
import type { ColumnsType } from 'antd/es/table';
import type { DomainServiceItem, ServiceApiKeyItem } from '../../types/domainService';
import McpKeyTab from './McpKeyTab';
import './ServiceConfigModal.css';

interface ServiceConfigModalProps {
  open: boolean;
  /** 空间服务写权限（can_write_space）：服务管理（含 MCP 接入 Tab）可见性 */
  canManage: boolean;
  srvConfigTarget: DomainServiceItem | null;
  apiKeys: ServiceApiKeyItem[];
  apiKeysLoading: boolean;
  creatingKey: boolean;
  onCreateKey: () => void;
  onDeleteKey: (keyId: string) => void;
  onCancel: () => void;
}

/** 服务状态 → 胶囊样式（active / testing / 其他） */
const statusTone = (status?: string): 'is-active' | 'is-testing' | 'is-idle' => {
  if (status === 'active') return 'is-active';
  if (status === 'testing') return 'is-testing';
  return 'is-idle';
};

export default function ServiceConfigModal({
  open,
  canManage,
  srvConfigTarget,
  apiKeys,
  apiKeysLoading,
  creatingKey,
  onCreateKey,
  onDeleteKey,
  onCancel,
}: ServiceConfigModalProps) {
  const { t } = useTranslation();
  // [jonex] 权限重构 B1：service:write 已删除。MCP 接入 Tab 改为跟随空间服务写权限
  // （can_write_space：owner / manager 成员 / 租户管理员），与页面服务管理按钮同源。
  const isAdmin = canManage;
  const [activeTab, setActiveTab] = useState<string>('api');
  const [copiedKeyId, setCopiedKeyId] = useState<string | null>(null);

  // ⚠️ API 接入元信息：暂为死数据，后续接入真实字段
  const apiStatusText =
    srvConfigTarget?.status === 'active'
      ? t('status.active')
      : srvConfigTarget?.status === 'testing'
        ? t('domainService.status.testing')
        : t('status.inactive');
  const apiEndpoint = srvConfigTarget ? `/api/v1/knowledge-base/services/${srvConfigTarget.id}` : '-';
  const apiMethod = 'POST';
  const apiAuth = 'API Key';

  const handleCopyKey = async (keyId: string, key: string) => {
    await copy(key);
    setCopiedKeyId(keyId);
    setTimeout(() => setCopiedKeyId(null), 2000);
  };

  const formatDate = (dateStr: string | null): string => {
    if (!dateStr) return '—';
    try {
      return new Date(dateStr).toISOString().slice(0, 10);
    } catch {
      return dateStr;
    }
  };

  const apiKeyColumns: ColumnsType<ServiceApiKeyItem> = [
    {
      title: t('domainManagement.apiKey'),
      dataIndex: 'key_encrypted',
      key: 'key_encrypted',
      render: (val: string, record: ServiceApiKeyItem) => (
        <span className="scm-key">
          <span className="scm-key-text">{val || '—'}</span>
          {val && (
            <Button
              type="text"
              size="small"
              className={`scm-copy${copiedKeyId === record.id ? ' is-copied' : ''}`}
              title={copiedKeyId === record.id ? t('common.copySuccess') : t('domainManagement.copyApiKey')}
              onClick={() => handleCopyKey(record.id, val)}
            >
              {copiedKeyId === record.id ? <CheckOutlined /> : <CopyOutlined />}
            </Button>
          )}
        </span>
      ),
    },
    {
      title: t('domainManagement.keyPrefix'),
      dataIndex: 'key_prefix',
      key: 'key_prefix',
      width: 132,
      render: (val: string) => (val ? <span className="scm-prefix">{val}</span> : '—'),
    },
    {
      title: t('domainManagement.expiresAt'),
      dataIndex: 'expires_at',
      key: 'expires_at',
      width: 108,
      render: (val: string | null) => <span className="scm-dim">{formatDate(val)}</span>,
    },
    {
      title: t('domainManagement.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 108,
      render: (val: string | null) => <span className="scm-dim">{formatDate(val)}</span>,
    },
    {
      title: t('domainManagement.status'),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 88,
      render: (v: number) =>
        v === 1 ? (
          <span className="scm-status is-active">{t('status.active')}</span>
        ) : (
          <span className="scm-status is-idle">{t('status.inactive')}</span>
        ),
    },
    {
      title: t('domainManagement.srvConfigActions'),
      key: 'actions',
      width: 84,
      align: 'right',
      render: (_: unknown, record: ServiceApiKeyItem) => (
        <Button type="text" size="small" danger className="scm-del" onClick={() => onDeleteKey(record.id)}>
          <DeleteOutlined /> {t('common.delete')}
        </Button>
      ),
    },
  ];

  return (
    <Modal
      wrapClassName="scm-modal"
      title={
        <span className="scm-title">
          <span className="scm-title-ico">
            <ApiOutlined />
          </span>
          {t('domainManagement.srvConfigTitle')}
        </span>
      }
      open={open}
      onCancel={onCancel}
      footer={
        <Button autoInsertSpace={false} onClick={onCancel}>
          {t('common.close')}
        </Button>
      }
      width={880}
      destroyOnHidden
    >
      {/* 服务身份：名称 + 状态，配一句用途说明 */}
      <div className="scm-hero">
        <span className="scm-hero-ico">
          <KeyOutlined />
        </span>
        <div className="scm-hero-main">
          <div className="scm-hero-name">
            <span className="scm-hero-name-text">{srvConfigTarget?.name || '—'}</span>
            <span className={`scm-status ${statusTone(srvConfigTarget?.status)}`}>{apiStatusText}</span>
          </div>
          <div className="scm-hero-sub">{t('domainManagement.srvConfigIntro')}</div>
        </div>
      </div>

      {/* 接入元信息：三段平铺，弱化边框，比重层 Card 更轻 */}
      <dl className="scm-meta">
        <div className="scm-meta-item is-wide">
          <dt>{t('domainManagement.apiEndpoint')}</dt>
          <dd>
            <Typography.Text code copyable className="scm-endpoint">
              {apiEndpoint}
            </Typography.Text>
          </dd>
        </div>
        <div className="scm-meta-item">
          <dt>{t('domainManagement.apiMethod')}</dt>
          <dd>
            <Tag color="blue" className="scm-tag">
              {apiMethod}
            </Tag>
          </dd>
        </div>
        <div className="scm-meta-item">
          <dt>{t('domainManagement.apiAuth')}</dt>
          <dd>
            <Tag className="scm-tag">{apiAuth}</Tag>
          </dd>
        </div>
      </dl>

      <Tabs
        className="scm-tabs"
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'api',
            label: t('domainManagement.apiAccessTitle'),
            children: (
              <div className="scm-section">
                <div className="scm-section-head">
                  <div className="scm-section-copy">
                    <h4>{t('domainManagement.apiKeyListTitle')}</h4>
                    <p>{t('domainManagement.apiKeyListDesc')}</p>
                  </div>
                  <Button
                    type="primary"
                    size="small"
                    icon={<PlusOutlined />}
                    loading={creatingKey}
                    onClick={onCreateKey}
                  >
                    {t('domainManagement.addApiKey')}
                  </Button>
                </div>
                <Table<ServiceApiKeyItem>
                  className="scm-table"
                  columns={apiKeyColumns}
                  dataSource={apiKeys}
                  rowKey="id"
                  pagination={false}
                  size="small"
                  loading={apiKeysLoading}
                  scroll={{ x: 760 }}
                  locale={{
                    emptyText: (
                      <Empty
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                        description={t('common.noApiKey')}
                        style={{ margin: '12px 0' }}
                      />
                    ),
                  }}
                />
              </div>
            ),
          },
          ...(isAdmin
            ? [
                {
                  key: 'mcp',
                  label: t('domainManagement.mcpAccessTitle'),
                  children: <McpKeyTab service={srvConfigTarget} visible={activeTab === 'mcp'} />,
                },
              ]
            : []),
        ]}
      />
    </Modal>
  );
}
