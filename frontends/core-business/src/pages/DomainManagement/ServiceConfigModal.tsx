import { useState } from 'react';
import { Modal, Button, Table, Tabs, Card, Descriptions, Tag, Typography } from 'antd';
import {
  ApiOutlined,
  PlusOutlined,
  CopyOutlined,
  CheckOutlined,
  DeleteOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import copy from 'copy-to-clipboard';
import type { ColumnsType } from 'antd/es/table';
import type { DomainServiceItem, ServiceApiKeyItem } from '../../types/domainService';
import { usePermission } from '@jonex/shared-lib';
import McpKeyTab from './McpKeyTab';

interface ServiceConfigModalProps {
  open: boolean;
  srvConfigTarget: DomainServiceItem | null;
  apiKeys: ServiceApiKeyItem[];
  apiKeysLoading: boolean;
  creatingKey: boolean;
  onCreateKey: () => void;
  onDeleteKey: (keyId: string) => void;
  onCancel: () => void;
}

export default function ServiceConfigModal({
  open,
  srvConfigTarget,
  apiKeys,
  apiKeysLoading,
  creatingKey,
  onCreateKey,
  onDeleteKey,
  onCancel,
}: ServiceConfigModalProps) {
  const { t } = useTranslation();
  // 权限码判断：MCP 接入 Tab 仅服务管理员（service:write）可见
  const { hasPerm } = usePermission();
  const isAdmin = hasPerm('service:write');
  const [activeTab, setActiveTab] = useState<string>('api');
  const [copiedKeyId, setCopiedKeyId] = useState<string | null>(null);

  // ⚠️ API 接入元信息：暂为死数据，后续接入真实字段
  const apiStatusText =
    srvConfigTarget?.status === 'active'
      ? t('status.active')
      : srvConfigTarget?.status === 'testing'
        ? t('domainService.status.testing')
        : t('status.inactive');
  const apiEndpoint = srvConfigTarget
    ? `/api/v1/knowledge-base/services/${srvConfigTarget.id}`
    : '-';
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
      width: 360,
      render: (val: string, record: ServiceApiKeyItem) => (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span className="yx-key-text">{val || '—'}</span>
          {val && (
            <Button
              type="text"
              className={`yx-copy-btn${copiedKeyId === record.id ? ' copied' : ''}`}
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
      width: 130,
      render: (val: string) => (val ? <Typography.Text code>{val}</Typography.Text> : '—'),
    },
    {
      title: t('domainManagement.expiresAt'),
      dataIndex: 'expires_at',
      key: 'expires_at',
      width: 120,
      render: (val: string | null) => formatDate(val),
    },
    {
      title: t('domainManagement.createdAt'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 120,
      render: (val: string | null) => formatDate(val),
    },
    {
      title: t('domainManagement.status'),
      dataIndex: 'is_active',
      key: 'is_active',
      width: 90,
      render: (v: number) => (v === 1 ? <Tag color="success">{t('status.active')}</Tag> : <Tag>{t('status.inactive')}</Tag>),
    },
    {
      title: t('domainManagement.srvConfigActions'),
      key: 'actions',
      width: 100,
      render: (_: unknown, record: ServiceApiKeyItem) => (
        <Button type="text" danger onClick={() => onDeleteKey(record.id)}>
          <DeleteOutlined /> {t('common.delete')}
        </Button>
      ),
    },
  ];

  return (
    <Modal
      wrapClassName="yx-domain-space-modal"
      title={
        <span>
          <ApiOutlined style={{ color: '#f97316', marginRight: 8 }} />
          {t('domainManagement.srvConfigTitle')}
        </span>
      }
      open={open}
      onCancel={onCancel}
      footer={<Button onClick={onCancel}>{t('common.cancel')}</Button>}
      width={760}
      destroyOnHidden
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'api',
            label: t('domainManagement.apiAccessTitle'),
            children: (
              <>
                <p style={{ fontSize: 14, color: '#475569', marginBottom: 12 }}>
                  {t('domainManagement.srvConfigDesc', {
                    name: srvConfigTarget?.name || '',
                  })}
                </p>

                {/* API 接入元信息（暂为死数据，无边框/上下排列/一行两个，无 title） */}
                <Card size="small" style={{ marginBottom: 16 }}>
                  <Descriptions column={2} layout="vertical" size="small" colon={false}>
                    <Descriptions.Item label={t('domainManagement.apiStatus')}>
                      <Tag color={srvConfigTarget?.status === 'active' ? 'success' : 'default'}>
                        {apiStatusText}
                      </Tag>
                    </Descriptions.Item>
                    <Descriptions.Item label={t('domainManagement.apiEndpoint')}>
                      <Typography.Text code copyable>
                        {apiEndpoint}
                      </Typography.Text>
                    </Descriptions.Item>
                    <Descriptions.Item label={t('domainManagement.apiMethod')}>
                      <Tag color="blue">{apiMethod}</Tag>
                    </Descriptions.Item>
                    <Descriptions.Item label={t('domainManagement.apiAuth')}>
                      <Tag>{apiAuth}</Tag>
                    </Descriptions.Item>
                  </Descriptions>
                </Card>

                {/* API Key 标题 + 描述 与 添加按钮 左右分布 */}
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 12,
                  }}
                >
                  <div>
                    <h4 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>
                      {t('domainManagement.apiKeyListTitle')}
                    </h4>
                    <p style={{ margin: '4px 0 0', fontSize: 13, color: '#64748b' }}>
                      {t('domainManagement.apiKeyListDesc')}
                    </p>
                  </div>
                  <Button type="primary" size="small" icon={<PlusOutlined />} loading={creatingKey} onClick={onCreateKey}>
                    {t('domainManagement.addApiKey')}
                  </Button>
                </div>

                {/* API Key 列表 */}
                <Table<ServiceApiKeyItem>
                  columns={apiKeyColumns}
                  dataSource={apiKeys}
                  rowKey="id"
                  pagination={false}
                  size="small"
                  loading={apiKeysLoading}
                  locale={{ emptyText: t('common.noApiKey') }}
                />
              </>
            ),
          },
          ...(isAdmin
            ? [
                {
                  key: 'mcp',
                  label: t('domainManagement.mcpAccessTitle'),
                  children: (
                    <McpKeyTab service={srvConfigTarget} visible={activeTab === 'mcp'} />
                  ),
                },
              ]
            : []),
        ]}
      />
    </Modal>
  );
}
