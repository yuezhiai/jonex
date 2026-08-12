import React, { useState, useImperativeHandle, forwardRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Drawer, Table, Tag, Typography, Spin, Result, Button, Empty } from 'antd';
import type { TableProps } from 'antd';
import { getAuthorizedKeys } from '@/api/mcpServices';
import type { McpServiceItem, AuthorizedKeyItem } from '@/api/mcpServices';
import './index.css';

/** 授权 MCP Key 抽屉对外暴露的句柄 */
export type AuthorizedKeysDrawerHandle = {
  /** 打开抽屉并加载指定服务的授权 Key 列表 */
  open: (record: McpServiceItem) => void;
};

interface AuthorizedKeysDrawerProps {}

const AuthorizedKeysDrawer = forwardRef<AuthorizedKeysDrawerHandle, AuthorizedKeysDrawerProps>((_props, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [record, setRecord] = useState<McpServiceItem | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [keys, setKeys] = useState<AuthorizedKeyItem[]>([]);

  /** 加载授权 Key 列表 */
  const loadKeys = async (serviceId: string) => {
    setLoading(true);
    setError(null);
    try {
      const result = await getAuthorizedKeys(serviceId);
      setKeys(result.items);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('mcpServiceDirectory.loadFailed'));
    } finally {
      setLoading(false);
    }
  };

  // 对外暴露打开方法：打开抽屉并加载数据
  useImperativeHandle(
    ref,
    () => ({
      open(rec) {
        setRecord(rec);
        setOpen(true);
        loadKeys(rec.id);
      },
    }),
    [t],
  );

  const columns: TableProps<AuthorizedKeyItem>['columns'] = [
    { title: t('mcpServiceDirectory.keyColumnName'), dataIndex: 'key_name', key: 'key_name' },
    {
      title: t('mcpServiceDirectory.keyColumnPrefix'),
      dataIndex: 'key_prefix',
      key: 'key_prefix',
      render: (v: string) => (v ? <Typography.Text code>{v}</Typography.Text> : '-'),
    },
    {
      title: t('mcpServiceDirectory.keyColumnPermission'),
      dataIndex: 'permission_level',
      key: 'permission_level',
      render: (v: string) =>
        v === '*' ? (
          <Tag color="gold">{t('mcpServiceDirectory.permissionAll')}</Tag>
        ) : (
          <Tag color="blue">{t('mcpServiceDirectory.permissionRead')}</Tag>
        ),
    },
    {
      title: t('mcpServiceDirectory.keyColumnOrg'),
      dataIndex: 'org_name',
      key: 'org_name',
      render: (v: string | null) => v || '-',
    },
    {
      title: t('mcpServiceDirectory.keyColumnStatus'),
      dataIndex: 'key_status',
      key: 'key_status',
      render: (v: string) =>
        v === 'active' ? (
          <Tag color="success">{t('mcpServiceDirectory.keyStatusActive')}</Tag>
        ) : (
          <Tag color="error">{t('mcpServiceDirectory.keyStatusRevoked')}</Tag>
        ),
    },
  ];

  return (
    <Drawer
      title={t('mcpServiceDirectory.authorizedKeysTitle', { name: record?.name || '-' })}
      placement="right"
      width={560}
      open={open}
      onClose={() => setOpen(false)}
    >
      {loading ? (
        <div className="mcp-svc-center-box">
          <Spin size="large" />
        </div>
      ) : error ? (
        <Result
          status="error"
          title={t('mcpServiceDirectory.loadFailed')}
          subTitle={error}
          extra={
            <Button type="primary" onClick={() => record && loadKeys(record.id)}>
              {t('common.retry')}
            </Button>
          }
        />
      ) : (
        <Table
          rowKey="key_id"
          columns={columns}
          dataSource={keys}
          pagination={false}
          locale={{ emptyText: <Empty description={t('mcpServiceDirectory.noAuthorizedKeys')} /> }}
        />
      )}
    </Drawer>
  );
});

AuthorizedKeysDrawer.displayName = 'AuthorizedKeysDrawer';

export default AuthorizedKeysDrawer;
