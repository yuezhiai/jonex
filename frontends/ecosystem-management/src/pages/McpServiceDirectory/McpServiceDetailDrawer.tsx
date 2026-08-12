import React, { useState, useMemo, useImperativeHandle, forwardRef } from 'react';
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
  Select,
  Input,
  Alert,
  Popconfirm,
  Typography,
} from 'antd';
import { PlusOutlined, PlusCircleOutlined } from '@ant-design/icons';
import type { TableProps } from 'antd';
import { getAuthorizedKeys } from '@/api/mcpServices';
import type { McpServiceItem, AuthorizedKeyItem } from '@/api/mcpServices';
import {
  addKeyToService,
  createKeyForService,
  updateKeyPermission,
  removeKeyFromService,
} from '@/api/domainServices';
import type { CreateKeyForServiceResponse } from '@/api/domainServices';
import { listMcpKeys } from '@/api/mcpKeys';
import type { McpKeyItem } from '@/api/mcpKeys';
import { safeMessage } from '@/utils/safeMessage';
import './index.css';

/** 服务详情抽屉对外暴露的句柄 */
export type McpServiceDetailDrawerHandle = {
  /** 打开抽屉并渲染指定服务的详情 */
  open: (record: McpServiceItem) => void;
};

interface McpServiceDetailDrawerProps {}

/** 格式化时间：YYYY-MM-DD HH:mm */
const formatDate = (dateStr?: string | null): string => {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return '-';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

/** 知识库 Tag 颜色循环（多知识库时按序取色区分） */
const KB_TAG_COLORS = ['blue', 'geekblue', 'purple', 'cyan', 'green', 'orange', 'gold', 'magenta'];

/** 授权 Key 状态 Tag */
const renderKeyStatus = (v: string, t: (key: string) => string) =>
  v === 'active' ? (
    <Tag color="success">{t('mcpServiceDirectory.keyStatusActive')}</Tag>
  ) : (
    <Tag color="error">{t('mcpServiceDirectory.keyStatusRevoked')}</Tag>
  );

/** 服务级权限 Tag：call=蓝色，view=金色 */
const renderPermissionLevel = (v: string, t: (key: string) => string) =>
  v === 'view' ? (
    <Tag color="gold">{t('mcpServiceDirectory.permissionView')}</Tag>
  ) : (
    <Tag color="blue">{t('mcpServiceDirectory.permissionCall')}</Tag>
  );

const McpServiceDetailDrawer = forwardRef<McpServiceDetailDrawerHandle, McpServiceDetailDrawerProps>((_props, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [record, setRecord] = useState<McpServiceItem | null>(null);
  const [keys, setKeys] = useState<AuthorizedKeyItem[]>([]);
  const [loadingKeys, setLoadingKeys] = useState(false);

  // ── 授权 Key 管理 ──
  const [allKeys, setAllKeys] = useState<McpKeyItem[]>([]);
  const [addKeyOpen, setAddKeyOpen] = useState(false);
  const [addKeyId, setAddKeyId] = useState('');
  const [addKeyPermission, setAddKeyPermission] = useState<'call' | 'view'>('call');
  const [addingKey, setAddingKey] = useState(false);
  const [createKeyOpen, setCreateKeyOpen] = useState(false);
  const [newKeyName, setNewKeyName] = useState('');
  const [newKeyDescription, setNewKeyDescription] = useState('');
  const [newKeyPermission, setNewKeyPermission] = useState<'call' | 'view'>('call');
  const [creatingKey, setCreatingKey] = useState(false);
  const [createKeyResult, setCreateKeyResult] = useState<CreateKeyForServiceResponse | null>(null);
  // 正在切换权限 / 移除的 key_id（loading 态）
  const [permLoading, setPermLoading] = useState<string | null>(null);
  const [removeLoading, setRemoveLoading] = useState<string | null>(null);

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

  /** 加载全部 MCP Key（用于「添加已有 Key」下拉，过滤已授权） */
  const loadAllKeys = async () => {
    try {
      const result = await listMcpKeys();
      setAllKeys(result.items ?? []);
    } catch (err: unknown) {
      console.warn('[McpServiceDetailDrawer] 加载全部 Key 失败', err);
      setAllKeys([]);
    }
  };

  // 对外暴露打开方法：立即渲染列表数据，并异步拉取授权 Key / 全部 Key
  useImperativeHandle(
    ref,
    () => ({
      open(rec) {
        setRecord(rec);
        setKeys([]);
        setCreateKeyResult(null);
        setAddKeyOpen(false);
        setCreateKeyOpen(false);
        setOpen(true);
        loadAuthorizedKeys(rec.id);
        loadAllKeys();
      },
    }),
    [],
  );

  // 已授权组织：从授权 Key 提取 org_name 去重
  const orgs = useMemo(() => {
    const set = new Set<string>();
    keys.forEach((k) => {
      if (k.org_name) set.add(k.org_name);
    });
    return Array.from(set);
  }, [keys]);

  // 「添加已有 Key」下拉：全部 Key 过滤掉已授权
  const availableKeys = useMemo(() => {
    const authedIds = new Set(keys.map((k) => k.key_id));
    return allKeys.filter((k) => !authedIds.has(k.id));
  }, [allKeys, keys]);

  /** 添加已有 Key 到服务授权 */
  const handleAddKey = async () => {
    if (!record || !addKeyId) return;
    setAddingKey(true);
    try {
      await addKeyToService(record.id, { key_id: addKeyId, permission_level: addKeyPermission });
      safeMessage.success(t('mcpServiceDirectory.addKeySuccess'));
      setAddKeyId('');
      setAddKeyOpen(false);
      loadAuthorizedKeys(record.id);
    } catch (err: unknown) {
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
    } finally {
      setAddingKey(false);
    }
  };

  /** 创建新 Key + 自动关联，成功后展示一次性明文 */
  const handleCreateKey = async () => {
    if (!record || !newKeyName.trim()) {
      safeMessage.warning(t('mcpServiceDirectory.keyNameRequired'));
      return;
    }
    setCreatingKey(true);
    try {
      const res = await createKeyForService(record.id, {
        name: newKeyName.trim(),
        permission_level: newKeyPermission,
        description: newKeyDescription.trim() || null,
      });
      setCreateKeyResult(res);
      loadAuthorizedKeys(record.id);
    } catch (err: unknown) {
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
    } finally {
      setCreatingKey(false);
    }
  };

  /** 切换授权 Key 权限（call ↔ view） */
  const handleTogglePermission = async (item: AuthorizedKeyItem) => {
    if (!record) return;
    const next: 'call' | 'view' = item.permission_level === 'call' ? 'view' : 'call';
    setPermLoading(item.key_id);
    try {
      await updateKeyPermission(record.id, item.key_id, { permission_level: next });
      safeMessage.success(t('mcpServiceDirectory.switchPermissionSuccess'));
      loadAuthorizedKeys(record.id);
    } catch (err: unknown) {
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
    } finally {
      setPermLoading(null);
    }
  };

  /** 移除单服务授权（不删除 Key） */
  const handleRemoveKey = async (item: AuthorizedKeyItem) => {
    if (!record) return;
    setRemoveLoading(item.key_id);
    try {
      await removeKeyFromService(record.id, item.key_id);
      safeMessage.success(t('mcpServiceDirectory.removeAuthSuccess'));
      loadAuthorizedKeys(record.id);
    } catch (err: unknown) {
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
    } finally {
      setRemoveLoading(null);
    }
  };

  // 授权信息：Key 列表（含操作列）
  const keyColumns: TableProps<AuthorizedKeyItem>['columns'] = [
    {
      title: t('mcpServiceDirectory.keyColumnPrefix'),
      dataIndex: 'key_prefix',
      key: 'key_prefix',
      render: (v: string) => (v ? <Tag>{v}</Tag> : '-'),
    },
    {
      title: t('mcpServiceDirectory.keyColumnName'),
      dataIndex: 'key_name',
      key: 'key_name',
      render: (v: string) => v || '-',
    },
    {
      title: t('mcpServiceDirectory.keyColumnPermission'),
      dataIndex: 'permission_level',
      key: 'permission_level',
      render: (v: string) => renderPermissionLevel(v, t),
    },
    {
      title: t('mcpServiceDirectory.colActions'),
      key: 'actions',
      render: (_: unknown, item: AuthorizedKeyItem) => (
        <Space size={0}>
          <Button
            type="link"
            size="small"
            loading={permLoading === item.key_id}
            onClick={() => handleTogglePermission(item)}
          >
            {t('mcpServiceDirectory.switchPermission')}
          </Button>
          <Popconfirm
            title={t('mcpServiceDirectory.removeAuthConfirm')}
            onConfirm={() => handleRemoveKey(item)}
          >
            <Button type="link" size="small" danger loading={removeLoading === item.key_id}>
              {t('mcpServiceDirectory.removeAuthorization')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Drawer
      title={t('mcpServiceDirectory.detailTitle', { name: record?.name || '-' })}
      placement="right"
      width={640}
      open={open}
      onClose={() => setOpen(false)}
    >
      {record && (
        <>
          {/* 基础信息 */}
          <h3 className="mcp-svc-detail-section">{t('mcpServiceDirectory.detailSectionBasic')}</h3>
          <Descriptions column={2} layout="vertical" size="small" colon={false}>
            <Descriptions.Item label={t('mcpServiceDirectory.colName')} span={2}>
              <span className="mcp-svc-name">{record.name}</span>
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpServiceDirectory.colSpace')}>{record.space_name || '-'}</Descriptions.Item>
            <Descriptions.Item label={t('mcpServiceDirectory.colDomainType')}>
              {record.domain_type || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpServiceDirectory.colEnabled')}>
              {record.enabled === 1 ? (
                <Tag color="success">{t('mcpServiceDirectory.enabledStatus')}</Tag>
              ) : (
                <Tag>{t('mcpServiceDirectory.disabledStatus')}</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpServiceDirectory.colStatus')}>
              {record.is_published ? (
                <Tag color="success">{t('mcpServiceDirectory.statusPublished')}</Tag>
              ) : (
                <Tag>{t('mcpServiceDirectory.statusUnpublished')}</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpServiceDirectory.colPublishedAt')}>
              {formatDate(record.published_at)}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpServiceDirectory.colPublishedBy')}>
              {record.published_by || '-'}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpServiceDirectory.detailCreatedAt')}>
              {formatDate(record.created_at)}
            </Descriptions.Item>
            <Descriptions.Item label={t('mcpServiceDirectory.detailLastCallAt')}>
              {formatDate(record.last_call_at)}
            </Descriptions.Item>
            <Descriptions.Item label={t('common.description')} span={2}>
              {record.description || t('mcpServiceDirectory.noDescription')}
            </Descriptions.Item>
          </Descriptions>

          {/* 知识来源 */}
          <Divider />
          <h3 className="mcp-svc-detail-section">{t('mcpServiceDirectory.detailSectionSources')}</h3>
          {record.kb_names && record.kb_names.length > 0 ? (
            <Space size={4} wrap>
              {record.kb_names.map((n, i) => (
                <Tag key={n} color={KB_TAG_COLORS[i % KB_TAG_COLORS.length]}>
                  {n}
                </Tag>
              ))}
            </Space>
          ) : (
            <span style={{ color: '#94a3b8' }}>{t('mcpServiceDirectory.noKb')}</span>
          )}

          {/* 授权信息（授权 Key 管理面板） */}
          <Divider />
          <h3 className="mcp-svc-detail-section">{t('mcpServiceDirectory.detailSectionAuth')}</h3>

          <Space size={8} style={{ marginBottom: 12 }}>
            <Button size="small" icon={<PlusOutlined />} onClick={() => setAddKeyOpen((v) => !v)}>
              {t('mcpServiceDirectory.addExistingKey')}
            </Button>
            <Button size="small" icon={<PlusCircleOutlined />} onClick={() => setCreateKeyOpen((v) => !v)}>
              {t('mcpServiceDirectory.createNewKey')}
            </Button>
          </Space>

          {/* 添加已有 Key 内联面板 */}
          {addKeyOpen && (
            <div className="mcp-svc-auth-inline">
              <Space wrap>
                <Select
                  placeholder={t('mcpServiceDirectory.selectKeyPlaceholder')}
                  style={{ width: 200 }}
                  value={addKeyId || undefined}
                  onChange={setAddKeyId}
                  options={availableKeys.map((k) => ({
                    value: k.id,
                    label: k.name || k.key_prefix || k.id,
                  }))}
                />
                <Select
                  style={{ width: 90 }}
                  value={addKeyPermission}
                  onChange={setAddKeyPermission}
                  options={[
                    { value: 'call', label: 'call' },
                    { value: 'view', label: 'view' },
                  ]}
                />
                <Button type="primary" size="small" loading={addingKey} onClick={handleAddKey}>
                  {t('common.confirm')}
                </Button>
              </Space>
            </div>
          )}

          {/* 创建新 Key 内联面板 */}
          {createKeyOpen && !createKeyResult && (
            <div className="mcp-svc-auth-inline">
              <Space direction="vertical" style={{ width: '100%' }} size={8}>
                <Input
                  placeholder={t('mcpServiceDirectory.keyNamePlaceholder')}
                  value={newKeyName}
                  onChange={(e) => setNewKeyName(e.target.value)}
                  maxLength={255}
                />
                <Input
                  placeholder={t('common.description')}
                  value={newKeyDescription}
                  onChange={(e) => setNewKeyDescription(e.target.value)}
                />
                <Space>
                  <Select
                    style={{ width: 90 }}
                    value={newKeyPermission}
                    onChange={setNewKeyPermission}
                    options={[
                      { value: 'call', label: 'call' },
                      { value: 'view', label: 'view' },
                    ]}
                  />
                  <Button type="primary" size="small" loading={creatingKey} onClick={handleCreateKey}>
                    {t('common.save')}
                  </Button>
                </Space>
              </Space>
            </div>
          )}

          {/* 创建成功：一次性明文展示 */}
          {createKeyResult && (
            <div className="mcp-svc-auth-plaintext">
              <Alert
                type="warning"
                showIcon
                message={t('mcpServiceDirectory.keyPlaintextWarning')}
                style={{ marginBottom: 10 }}
              />
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Input.Password readOnly value={createKeyResult.plaintext} style={{ flex: 1 }} />
                <Typography.Text
                  copyable={{
                    text: createKeyResult.plaintext,
                    tooltips: [t('mcpServiceDirectory.copy'), t('common.copySuccess')],
                  }}
                />
              </div>
            </div>
          )}

          <Spin spinning={loadingKeys} style={{ marginTop: 12 }}>
            {keys.length > 0 ? (
              <Table rowKey="key_id" columns={keyColumns} dataSource={keys} pagination={false} size="small" bordered />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpServiceDirectory.noAuthorizedKeysHint')} />
            )}
          </Spin>

          {/* 已授权组织列表 */}
          <Divider />
          <h3 className="mcp-svc-detail-section">{t('mcpServiceDirectory.detailSectionOrgs')}</h3>
          {orgs.length > 0 ? (
            <Space size={4} wrap>
              {orgs.map((o) => (
                <Tag key={o}>{o}</Tag>
              ))}
            </Space>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('mcpServiceDirectory.detailNoOrgs')} />
          )}
        </>
      )}
    </Drawer>
  );
});

McpServiceDetailDrawer.displayName = 'McpServiceDetailDrawer';

export default McpServiceDetailDrawer;
