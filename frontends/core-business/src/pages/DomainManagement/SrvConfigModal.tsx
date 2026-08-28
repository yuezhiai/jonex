import React, { useState } from 'react';
import { Modal, Button, Table, Tabs } from 'antd';
import { KeyOutlined, PlusOutlined, CopyOutlined, CheckOutlined, DeleteOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import copy from 'copy-to-clipboard';
import type { DomainServiceItem, ServiceApiKeyItem } from '../../types/domainService';
import { usePermission } from '@jonex/shared-lib';
import McpKeyTab from './McpKeyTab';

interface SrvConfigModalProps {
  open: boolean;
  srvConfigTarget: DomainServiceItem | null;
  apiKeys: ServiceApiKeyItem[];
  apiKeysLoading: boolean;
  creatingKey: boolean;
  onCreateKey: () => void;
  onDeleteKey: (keyId: string) => void;
  onCancel: () => void;
}

export default function SrvConfigModal({
  open,
  srvConfigTarget,
  apiKeys,
  apiKeysLoading,
  creatingKey,
  onCreateKey,
  onDeleteKey,
  onCancel,
}: SrvConfigModalProps) {
  const { t } = useTranslation();
  // 权限码判断：MCP Key Tab 仅服务管理员（service:write）可见
  const { hasPerm } = usePermission();
  const isAdmin = hasPerm('service:write');
  const [activeTab, setActiveTab] = useState<string>('apiKey');
  const [copiedKeyId, setCopiedKeyId] = useState<string | null>(null);

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

  return (
    <Modal
      wrapClassName="yx-domain-space-modal"
      title={
        <span>
          <KeyOutlined style={{ color: '#f97316', marginRight: 8 }} />
          {t('domainManagement.srvConfigTitle')}
        </span>
      }
      open={open}
      onCancel={onCancel}
      footer={<Button onClick={onCancel}>{t('common.cancel')}</Button>}
      width={760}
    >
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'apiKey',
            label: t('domainManagement.apiKey'),
            children: (
              <>
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 12,
                  }}
                >
                  <p style={{ fontSize: 14, color: '#475569', margin: 0 }}>
                    {t('domainManagement.srvConfigDesc', {
                      name: srvConfigTarget?.name || '',
                    })}
                  </p>
                  <Button type="primary" size="small" icon={<PlusOutlined />} loading={creatingKey} onClick={onCreateKey}>
                    {t('domainManagement.addApiKey')}
                  </Button>
                </div>
                <Table<ServiceApiKeyItem>
                  columns={[
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
                      title: t('domainManagement.expiresAt'),
                      dataIndex: 'expires_at',
                      key: 'expires_at',
                      width: 120,
                      render: (val: string | null) => formatDate(val),
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
                  ]}
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
                  key: 'mcpKey',
                  label: t('mcpKeyManagement.tabLabel'),
                  children: (
                    <McpKeyTab service={srvConfigTarget} visible={activeTab === 'mcpKey'} />
                  ),
                },
              ]
            : []),
        ]}
      />
    </Modal>
  );
}
