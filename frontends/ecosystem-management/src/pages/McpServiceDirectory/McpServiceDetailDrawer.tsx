import React, { useState, useImperativeHandle, forwardRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Drawer,
  Descriptions,
  Tag,
  Space,
  Spin,
  Divider,
  Table,
  Empty,
  Button,
  Typography,
} from 'antd';
import type { TableProps } from 'antd';
import { getAuthorizedKeys, getMcpServiceDetail } from '@/api/mcpServices';
import type { McpServiceItem, AuthorizedKeyItem, ServiceAccessInfo } from '@/api/mcpServices';
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
    const [access, setAccess] = useState<ServiceAccessInfo | null>(null);

    /** 服务三态：disabled（停用）> published / unpublished（由 is_published 决定） */
    const serviceStatus = (r: McpServiceItem) =>
      r.enabled === 0 ? 'disabled' : r.is_published ? 'published' : 'unpublished';

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

    /** 加载服务接入信息（详情接口 access 字段；成功后以详情覆盖列表项，失败静默降级为 null） */
    const loadAccess = async (serviceId: string) => {
      try {
        const detail = await getMcpServiceDetail(serviceId);
        setRecord(detail);
        setAccess(detail.access ?? null);
      } catch (err: unknown) {
        console.warn('[McpServiceDetailDrawer] 加载接入信息失败', err);
        setAccess(null);
      }
    };

    // 对外暴露打开方法：立即渲染列表数据，并异步拉取授权 Key 与接入信息
    useImperativeHandle(
      ref,
      () => ({
        open(rec) {
          setRecord(rec);
          setKeys([]);
          setAccess(null);
          setOpen(true);
          loadAuthorizedKeys(rec.id);
          loadAccess(rec.id);
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

    // 接入信息派生：API 接入（系统写服务 access.api=null → 仅 MCP 接入）/ MCP 接入（server_url 由后端 access.mcp.server_url 下发）
    const api = access?.api;
    const mcp = access?.mcp;
    const isSystem = record?.service_type === 'system';

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
                {record.tool || <span style={{ color: '#94a3b8' }}>-</span>}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.colToolDesc')} span={2}>
                {record.tool_description || t('mcpServiceDirectory.noDescription')}
              </Descriptions.Item>
              {/* 能力类型：系统内置 → 知识写入；领域服务 → 领域服务 */}
              <Descriptions.Item label={t('mcpServiceDirectory.colCapabilityType')} span={2}>
                {record.service_type === 'system' ? (
                  <Tag color="blue">{t('mcpServiceDirectory.capabilityWrite')}</Tag>
                ) : (
                  <Tag>{t('mcpServiceDirectory.capabilityDomain')}</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.colSpace')}>
                {record.space_name || '-'}
              </Descriptions.Item>
              {/* MCP 状态三态：disabled（停用）> published / unpublished（由 is_published 决定） */}
              <Descriptions.Item label={t('mcpServiceDirectory.colStatus')}>
                {serviceStatus(record) === 'disabled' ? (
                  <Tag>{t('mcpServiceDirectory.statusDisabled')}</Tag>
                ) : record.is_published ? (
                  <Tag color="success">{t('mcpServiceDirectory.statusPublished')}</Tag>
                ) : (
                  <Tag>{t('mcpServiceDirectory.statusUnpublished')}</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.detailKbNames')} span={2}>
                {record.service_type === 'system' ? (
                  // 知识写入服务无预授权知识库，资源范围由 Key 授权决定
                  <span style={{ color: '#94a3b8' }}>{t('mcpServiceDirectory.scopeByKey')}</span>
                ) : record.kb_names && record.kb_names.length > 0 ? (
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

            {/* API 接入（详情接口 access.api；系统写服务无 REST query 语义 → 提示仅 MCP 接入） */}
            <Divider />
            <h3 className="mcp-svc-detail-section">{t('mcpServiceDirectory.detailSectionApiAccess')}</h3>
            {isSystem || !api ? (
              <Typography.Text type="secondary">
                {isSystem ? t('mcpServiceDirectory.accessApiOnlyMcp') : t('mcpServiceDirectory.accessApiNoInfo')}
              </Typography.Text>
            ) : (
              <Descriptions column={1} size="small" colon={false}>
                <Descriptions.Item label={t('mcpServiceDirectory.accessApiEndpoint')}>
                  <Typography.Text code>{api.endpoint}</Typography.Text>
                </Descriptions.Item>
                <Descriptions.Item label={t('mcpServiceDirectory.accessApiMethod')}>
                  <Tag color="blue">{api.method}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label={t('mcpServiceDirectory.accessApiAuth')}>
                  <Tag color="geekblue">{t('mcpServiceDirectory.accessAuthApiKey')}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label={t('mcpServiceDirectory.accessApiSamplePayload')}>
                  <pre
                    style={{
                      margin: 0,
                      padding: 8,
                      background: '#f6f8fa',
                      borderRadius: 6,
                      fontSize: 12,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-all',
                    }}
                  >
                    {JSON.stringify(api.sample_payload, null, 2)}
                  </pre>
                </Descriptions.Item>
              </Descriptions>
            )}

            {/* MCP 接入（详情接口 access.mcp；server_url 由后端 MCP_SERVER_PUBLIC_URL 配置下发） */}
            <Divider />
            <h3 className="mcp-svc-detail-section">{t('mcpServiceDirectory.detailSectionMcpAccess')}</h3>
            {!mcp ? (
              <Typography.Text type="secondary">{t('mcpServiceDirectory.accessMcpNoInfo')}</Typography.Text>
            ) : (
              <Descriptions column={1} size="small" colon={false}>
                <Descriptions.Item label={t('mcpServiceDirectory.accessMcpTransport')}>
                  <Tag>{mcp.transport}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label={t('mcpServiceDirectory.accessMcpToolName')}>
                  <Typography.Text code>{mcp.tool_name || record.tool || '-'}</Typography.Text>
                </Descriptions.Item>
                <Descriptions.Item label={t('mcpServiceDirectory.accessMcpAuth')}>
                  <Tag color="geekblue">{t('mcpServiceDirectory.accessAuthBearer')}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label={t('mcpServiceDirectory.accessMcpServerUrl')}>
                  {mcp?.server_url || <span style={{ color: '#94a3b8' }}>-</span>}
                </Descriptions.Item>
              </Descriptions>
            )}

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
              <Typography.Link
                onClick={() => {
                  setOpen(false);
                  onGoToKeys?.();
                }}
              >
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
