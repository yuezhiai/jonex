import React, { useState, useImperativeHandle, forwardRef, useEffect } from 'react';
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
  Form,
  Input,
  message,
} from 'antd';
import type { TableProps } from 'antd';
import {
  getAuthorizedKeys,
  getMcpServiceDetail,
  saveToolConfig,
  type SaveToolConfigParams,
} from '@/api/mcpServices';
import type { McpServiceItem, AuthorizedKeyItem, ServiceAccessInfo } from '@/api/mcpServices';
import { formatDateTime, usePermission } from '@jonex/shared-lib';
import './index.css';

/** 服务详情抽屉对外暴露的句柄 */
export type McpServiceDetailDrawerHandle = {
  /** 打开抽屉并渲染指定服务的详情 */
  open: (record: McpServiceItem) => void;
};

interface McpServiceDetailDrawerProps {
  /** 点击「前往 MCP Key 管理」时回调（切换到服务访问Key 视图） */
  onGoToKeys?: () => void;
  /** 保存 Tool 配置成功后回调（父级刷新列表，发布可用性依赖已保存的 Tool） */
  onSaved?: () => void;
}

const McpServiceDetailDrawer = forwardRef<McpServiceDetailDrawerHandle, McpServiceDetailDrawerProps>(
  ({ onGoToKeys, onSaved }, ref) => {
    const { t } = useTranslation();
    // 权限（shell 下发）：与列表页同一套 usePermission，写操作要求 mcp:service:manage
    const { permissions: userPermissions } = usePermission();
    const canManage = userPermissions.includes('mcp:service:manage');
    const [open, setOpen] = useState(false);
    const [record, setRecord] = useState<McpServiceItem | null>(null);
    const [keys, setKeys] = useState<AuthorizedKeyItem[]>([]);
    const [loadingKeys, setLoadingKeys] = useState(false);
    const [access, setAccess] = useState<ServiceAccessInfo | null>(null);
    const [savingTool, setSavingTool] = useState(false);
    const [toolForm] = Form.useForm<SaveToolConfigParams>();

    /** 发布态：与后端 status 一致（published / stopped / unpublished），缺省按 is_published 回退 */
    const serviceStatus = (r: McpServiceItem): 'published' | 'stopped' | 'unpublished' => {
      if (r.status === 'stopped') return 'stopped';
      if (r.status === 'unpublished' || !r.is_published) return 'unpublished';
      return 'published';
    };

    /** 详情数据变化时回填 Tool 表单（列表项 / 详情接口覆盖 / 保存成功刷新均走这里） */
    useEffect(() => {
      toolForm.setFieldsValue({
        tool: record?.tool || '',
        tool_description: record?.tool_description || '',
      });
    }, [record?.id, record?.tool, record?.tool_description, toolForm]);

    /** 保存 Tool 配置：成功后重新拉详情并通知父级刷新列表 */
    const handleSaveTool = async (values: SaveToolConfigParams) => {
      if (!record) return;
      setSavingTool(true);
      try {
        await saveToolConfig(record.id, {
          tool: values.tool.trim(),
          tool_description: values.tool_description?.trim() || null,
        });
        message.success(t('mcpServiceDirectory.toolSaveSuccess'));
        await loadAccess(record.id);
        onSaved?.();
      } catch (err: unknown) {
        message.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
      } finally {
        setSavingTool(false);
      }
    };

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
        // 统一 Key 前缀是 yxm_（mcp_key_service.py: plaintext[4:12]），
        // 这里补前缀只为可读，不能写成 mcp_ 以免与真实 Key 对不上。
        render: (v: string) => (v ? <Typography.Text code>yxm_{v}</Typography.Text> : '-'),
      },
      {
        title: t('mcpServiceDirectory.keyColumnStatus'),
        dataIndex: 'key_status',
        key: 'key_status',
        render: (v: string) => {
          const statusMeta: Record<string, { color: string; label: string }> = {
            active: { color: 'success', label: t('mcpServiceDirectory.keyStatusActive') },
            revoked: { color: 'error', label: t('mcpServiceDirectory.keyStatusRevoked') },
            expired: { color: 'warning', label: t('mcpServiceDirectory.keyStatusExpired') },
            disabled: { color: 'default', label: t('mcpServiceDirectory.keyStatusDisabled') },
          };
          const meta = statusMeta[v] ?? { color: 'default', label: v };
          return <Tag color={meta.color}>{meta.label}</Tag>;
        },
      },
      {
        title: t('mcpServiceDirectory.keyColumnExpiry'),
        dataIndex: 'expires_at',
        key: 'expires_at',
        render: (_: unknown, item: AuthorizedKeyItem) =>
          item.expires_at ? formatDateTime(item.expires_at) : t('mcpServiceDirectory.expiryForever'),
      },
    ];

    // 接入信息派生：API 接入（系统写服务 access.api=null → 仅 MCP 接入）/ MCP 接入（server_url 由后端 access.mcp.server_url 下发）
    const api = access?.api;
    const mcp = access?.mcp;
    // 系统写服务（固定合成 ID）Tool 固定、不可编辑
    const isSystem = record?.service_type === 'system' || record?.id === 'system.knowledge_document_write';

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
                {record.tool || <span style={{ color: 'var(--yx-text-muted)' }}>-</span>}
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
              {/* MCP 发布态三态：stopped / unpublished / published；服务本体停用（enabled=0）另标「当前不可调用」 */}
              <Descriptions.Item label={t('mcpServiceDirectory.colStatus')}>
                {serviceStatus(record) === 'stopped' ? (
                  <Tag color="warning">{t('mcpServiceDirectory.statusStopped')}</Tag>
                ) : serviceStatus(record) === 'unpublished' ? (
                  <Tag>{t('mcpServiceDirectory.statusUnpublished')}</Tag>
                ) : record.enabled === 0 ? (
                  <Tag>{t('mcpServiceDirectory.statusUnavailable')}</Tag>
                ) : (
                  <Tag color="success">{t('mcpServiceDirectory.statusPublished')}</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t('mcpServiceDirectory.detailKbNames')} span={2}>
                {record.service_type === 'system' ? (
                  // 知识写入服务无预授权知识库，资源范围由 Key 授权决定
                  t('mcpServiceDirectory.scopeByKey')
                ) : record.kb_names && record.kb_names.length > 0 ? (
                  <Space size={8} wrap>
                    {record.kb_names.map((n) => (
                      <Tag key={n} className="mcp-svc-kb-tag">
                        {n}
                      </Tag>
                    ))}
                  </Space>
                ) : (
                  t('mcpServiceDirectory.noKb')
                )}
              </Descriptions.Item>
            </Descriptions>

            {/* Tool 配置（仅领域服务 + 有管理权限时可见；系统写服务 Tool 固定不可改） */}
            {!isSystem && canManage && (
              <>
                <Divider />
                <h3 className="mcp-svc-detail-section">{t('mcpServiceDirectory.detailSectionTool')}</h3>
                <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 12 }}>
                  {t('mcpServiceDirectory.toolConfigHint')}
                </Typography.Text>
                <Form form={toolForm} layout="vertical" onFinish={handleSaveTool}>
                  <Form.Item
                    name="tool"
                    label={t('mcpServiceDirectory.toolNameLabel')}
                    rules={[
                      { required: true, whitespace: true, message: t('mcpServiceDirectory.toolNameRequired') },
                      { pattern: /^[A-Za-z0-9_]+$/, message: t('mcpServiceDirectory.toolNamePattern') },
                      { max: 128, message: t('mcpServiceDirectory.toolNameMax') },
                    ]}
                  >
                    <Input
                      placeholder={t('mcpServiceDirectory.toolNamePlaceholder')}
                      maxLength={128}
                      allowClear
                    />
                  </Form.Item>
                  <Form.Item name="tool_description" label={t('mcpServiceDirectory.toolDescriptionLabel')}>
                    <Input.TextArea
                      placeholder={t('mcpServiceDirectory.toolDescriptionPlaceholder')}
                      rows={2}
                      maxLength={255}
                    />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" loading={savingTool}>
                    {t('mcpServiceDirectory.toolSaveBtn')}
                  </Button>
                </Form>
              </>
            )}

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
                  {mcp?.server_url || <span style={{ color: 'var(--yx-text-muted)' }}>-</span>}
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
