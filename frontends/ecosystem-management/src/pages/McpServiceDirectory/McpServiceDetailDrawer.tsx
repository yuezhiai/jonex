import React, { useState, useImperativeHandle, forwardRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Drawer, Descriptions, Tag, Space, Spin, Divider, Table, Empty, Button, Typography } from 'antd';
import type { TableProps } from 'antd';
import { getAuthorizedKeys } from '@/api/mcpServices';
import type { McpServiceItem, AuthorizedKeyItem } from '@/api/mcpServices';
import './index.css';

/** 服务详情抽屉对外暴露的句柄 */
export type McpServiceDetailDrawerHandle = {
  /** 打开抽屉并渲染指定服务的详情 */
  open: (record: McpServiceItem) => void;
};

interface McpServiceDetailDrawerProps {
  /** 点击「前往 MCP Key 管理」时回调（切换到服务访问Key 视图） */
  onGoToKeys?: () => void;
}

/** 格式化时间：YYYY-MM-DD HH:mm */
const formatDate = (dateStr?: string | null): string => {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return '-';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

const McpServiceDetailDrawer = forwardRef<McpServiceDetailDrawerHandle, McpServiceDetailDrawerProps>(
  ({ onGoToKeys }, ref) => {
    const { t } = useTranslation();
    const [open, setOpen] = useState(false);
    const [record, setRecord] = useState<McpServiceItem | null>(null);
    const [keys, setKeys] = useState<AuthorizedKeyItem[]>([]);
    const [loadingKeys, setLoadingKeys] = useState(false);

    /** 加载指定服务的授权 Key 列表（失败静默降级为空，不阻塞抽屉） */
    const loadAuthorizedKeys = async (serviceId: string) => {
      setLoadingKeys(true);
      try {
        const result = await getAuthorizedKeys(serviceId);
        setKeys(result.items ?? []);
      } catch (err: unknown) {
        console.warn('[McpServiceDetailDrawer] 加载授权 Key 失败', err);
        setKeys([]);
      } finally {
        setLoadingKeys(false);
      }
    };

    // 对外暴露打开方法：立即渲染列表数据，并异步拉取授权 Key
    useImperativeHandle(
      ref,
      () => ({
        open(rec) {
          setRecord(rec);
          setKeys([]);
          setOpen(true);
          loadAuthorizedKeys(rec.id);
        },
      }),
      [],
    );

    // 访问授权表格：Key 名称 / 前缀 / 状态 / 有效期（纯展示）
    const keyColumns: TableProps<AuthorizedKeyItem>['columns'] = [
      {
        title: t('mcpServiceDirectory.keyColumnName'),
        dataIndex: 'key_name',
        key: 'key_name',
        render: (v: string) => v || '-',
      },
      {
        title: t('mcpServiceDirectory.keyColumnPrefix'),
        dataIndex: 'key_prefix',
        key: 'key_prefix',
        render: (v: string) => (v ? <Typography.Text code>mcp_{v}</Typography.Text> : '-'),
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
      {
        title: t('mcpServiceDirectory.keyColumnExpiry'),
        dataIndex: 'expires_at',
        key: 'expires_at',
        // TODO: 后端 authorized-keys 暂未返回 expires_at，当前统一按「永久有效」展示，待后端补充后接入
        render: (_: unknown, item: AuthorizedKeyItem) =>
          item.expires_at ? formatDate(item.expires_at) : t('mcpServiceDirectory.expiryForever'),
      },
    ];

    return (
      <Drawer
        className="mcp-svc-detail"
        title={t('mcpServiceDirectory.detailTitle')}
        placement="right"
        width={680}
        open={open}
        onClose={() => setOpen(false)}
        footer={
          <div style={{ textAlign: 'right' }}>
            <Button onClick={() => setOpen(false)}>{t('mcpServiceDirectory.detailClose')}</Button>
          </div>
        }
      >
        {record && (
          <>
            {/* 服务信息 */}
            <h3 className="mcp-svc-detail-section">{t('mcpServiceDirectory.detailSectionBasic')}</h3>
            <Descriptions column={2} size="small" colon={false} layout="vertical">
              <Descriptions.Item label={t('mcpServiceDirectory.colName')}>{record.name}</Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.colToolName')}>
                {record.tool_name || <span style={{ color: '#94a3b8' }}>-</span>}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.colToolDesc')} span={2}>
                {record.description || t('mcpServiceDirectory.noDescription')}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.colSpace')}>
                {record.space_name || '-'}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.colStatus')}>
                {record.is_published ? (
                  <Tag color="success">{t('mcpServiceDirectory.statusPublished')}</Tag>
                ) : (
                  <Tag>{t('mcpServiceDirectory.statusUnpublished')}</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.detailKbNames')} span={2}>
                {record.kb_names && record.kb_names.length > 0 ? (
                  <Space size={8} wrap>
                    {record.kb_names.map((n) => (
                      <Tag key={n} className="mcp-svc-kb-tag">
                        {n}
                      </Tag>
                    ))}
                  </Space>
                ) : (
                  <span style={{ color: '#94a3b8' }}>{t('mcpServiceDirectory.noKb')}</span>
                )}
              </Descriptions.Item>
            </Descriptions>

            {/* 访问授权 */}
            <Divider />
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: 12,
              }}
            >
              <h3 className="mcp-svc-detail-section" style={{ margin: 0 }}>
                {t('mcpServiceDirectory.detailSectionAuth')}
              </h3>
              <Typography.Link onClick={onGoToKeys}>
                {t('mcpServiceDirectory.goToKeyManagement')}
              </Typography.Link>
            </div>

            <Spin spinning={loadingKeys}>
              <Table
                rowKey="key_id"
                columns={keyColumns}
                dataSource={keys}
                size="small"
                bordered
                pagination={{
                  pageSize: 5,
                  total: keys.length,
                  showTotal: (total) => t('mcpServiceDirectory.totalCount', { count: total }),
                }}
                locale={{
                  emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpServiceDirectory.noAuthorizedKeysHint')} />,
                }}
              />
            </Spin>
          </>
        )}
      </Drawer>
    );
  },
);

McpServiceDetailDrawer.displayName = 'McpServiceDetailDrawer';

export default McpServiceDetailDrawer;
