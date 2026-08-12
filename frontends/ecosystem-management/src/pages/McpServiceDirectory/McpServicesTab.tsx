import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Table, Tag, Input, Select, Button, Tooltip, Modal, Space, Empty } from 'antd';
import { SearchOutlined, SyncOutlined } from '@ant-design/icons';
import type { TableProps } from 'antd';
import { listMcpServices, syncMcpServices, publishService, unpublishService } from '@/api/mcpServices';
import type { McpServiceItem } from '@/api/mcpServices';
import { enableDomainService, disableDomainService } from '@/api/domainServices';
import { safeMessage } from '@/utils/safeMessage';
import TestCallModal, { type TestCallModalHandle } from './TestCallModal';
import AuthorizedKeysDrawer, { type AuthorizedKeysDrawerHandle } from './AuthorizedKeysDrawer';
import McpServiceDetailDrawer, { type McpServiceDetailDrawerHandle } from './McpServiceDetailDrawer';
import './index.css';

/** 格式化时间：YYYY-MM-DD HH:mm */
const formatDate = (dateStr?: string | null): string => {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return '-';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

/** MCP 服务目录 — 服务列表 Tab（发布 / 取消发布 / 测试调用 / 授权 Key） */
export default function McpServicesTab() {
  const { t } = useTranslation();
  const [items, setItems] = useState<McpServiceItem[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [loading, setLoading] = useState(false);

  // 筛选条件
  const [search, setSearch] = useState('');
  const [spaceId, setSpaceId] = useState('');
  const [status, setStatus] = useState('');

  const testCallRef = useRef<TestCallModalHandle>(null);
  const keysDrawerRef = useRef<AuthorizedKeysDrawerHandle>(null);
  const detailDrawerRef = useRef<McpServiceDetailDrawerHandle>(null);

  const hasFilter = Boolean(search || spaceId || status);

  /** 加载列表（携带当前筛选条件） */
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await listMcpServices({
        search: search || undefined,
        space_id: spaceId || undefined,
        status: status || undefined,
      });
      setItems(result.items);
    } catch (err: unknown) {
      // 接口报错：提示错误信息并清空数据，不渲染 error DOM
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.loadFailed'));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [search, spaceId, status]);

  useEffect(() => {
    load();
  }, [load]);

  /** 空间筛选项：从当前列表聚合去重（space_id → space_name） */
  const spaceOptions = useMemo(() => {
    const map = new Map<string, string>();
    items.forEach((item) => {
      if (item.space_id && !map.has(item.space_id)) {
        map.set(item.space_id, item.space_name || item.space_id);
      }
    });
    return Array.from(map.entries()).map(([value, label]) => ({ value, label }));
  }, [items]);

  /** 手动从 knowledge_base 同步服务到发布表 */
  const handleSync = async () => {
    setSyncing(true);
    try {
      const result = await syncMcpServices();
      safeMessage.success(t('mcpServiceDirectory.syncSuccess', { count: result.synced_count }));
      load();
    } catch (err: unknown) {
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.syncFailed'));
    } finally {
      setSyncing(false);
    }
  };

  /** 发布领域服务 */
  const handlePublish = async (record: McpServiceItem) => {
    try {
      await publishService(record.id);
      safeMessage.success(t('mcpServiceDirectory.publishSuccess'));
      load();
    } catch (err: unknown) {
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
    }
  };

  /** 取消发布（二次确认） */
  const handleUnpublish = (record: McpServiceItem) => {
    Modal.confirm({
      title: t('mcpServiceDirectory.unpublish'),
      content: t('mcpServiceDirectory.unpublishConfirm', { name: record.name }),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await unpublishService(record.id);
          safeMessage.success(t('mcpServiceDirectory.unpublishSuccess'));
          load();
        } catch (err: unknown) {
          safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
        }
      },
    });
  };

  /** 启用领域服务（服务本体可用，与 MCP 发布独立） */
  const handleEnable = async (record: McpServiceItem) => {
    try {
      await enableDomainService(record.id);
      safeMessage.success(t('mcpServiceDirectory.enableSuccess'));
      load();
    } catch (err: unknown) {
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
    }
  };

  /** 停用领域服务（二次确认） */
  const handleDisable = (record: McpServiceItem) => {
    Modal.confirm({
      title: t('mcpServiceDirectory.disable'),
      content: t('mcpServiceDirectory.disableConfirm', { name: record.name }),
      okText: t('mcpServiceDirectory.disable'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await disableDomainService(record.id);
          safeMessage.success(t('mcpServiceDirectory.disableSuccess'));
          load();
        } catch (err: unknown) {
          safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.operationFailed'));
        }
      },
    });
  };

  const columns: TableProps<McpServiceItem>['columns'] = [
    {
      title: t('mcpServiceDirectory.colName'),
      dataIndex: 'name',
      key: 'name',
      render: (name: string) => <span className="mcp-svc-name">{name}</span>,
    },
    {
      title: t('mcpServiceDirectory.colSpace'),
      dataIndex: 'space_name',
      key: 'space_name',
      render: (v: string) => (v ? <Tag>{v}</Tag> : '-'),
    },
    {
      title: t('mcpServiceDirectory.colDomainType'),
      dataIndex: 'domain_type',
      key: 'domain_type',
      render: (v: string | null) => (v ? <Tag color="geekblue">{v}</Tag> : '-'),
    },
    {
      title: t('mcpServiceDirectory.colKbCount'),
      dataIndex: 'kb_count',
      key: 'kb_count',
      align: 'center',
      render: (count: number, record: McpServiceItem) =>
        record.kb_names && record.kb_names.length > 0 ? (
          <Tooltip title={record.kb_names.join('、')}>
            <span>{count}</span>
          </Tooltip>
        ) : (
          count
        ),
    },
    {
      title: t('mcpServiceDirectory.colStatus'),
      dataIndex: 'is_published',
      key: 'is_published',
      render: (v: boolean) =>
        v ? (
          <Tag color="success">{t('mcpServiceDirectory.statusPublished')}</Tag>
        ) : (
          <Tag>{t('mcpServiceDirectory.statusUnpublished')}</Tag>
        ),
    },
    {
      title: t('mcpServiceDirectory.colEnabled'),
      dataIndex: 'enabled',
      key: 'enabled',
      render: (v: number) =>
        v === 1 ? (
          <Tag color="success">{t('mcpServiceDirectory.enabledStatus')}</Tag>
        ) : (
          <Tag>{t('mcpServiceDirectory.disabledStatus')}</Tag>
        ),
    },
    {
      title: t('mcpServiceDirectory.colPublishedAt'),
      dataIndex: 'published_at',
      key: 'published_at',
      render: (v: string | null) => formatDate(v),
    },
    {
      title: t('mcpServiceDirectory.colPublishedBy'),
      dataIndex: 'published_by',
      key: 'published_by',
      render: (v: string | null) => v || '-',
    },
    {
      title: t('mcpServiceDirectory.colActions'),
      key: 'actions',
      render: (_: unknown, record: McpServiceItem) => (
        <Space size={0}>
          <Button type="link" size="small" onClick={() => detailDrawerRef.current?.open(record)}>
            {t('mcpServiceDirectory.detail')}
          </Button>
          {record.enabled === 1 ? (
            <Button type="link" size="small" danger onClick={() => handleDisable(record)}>
              {t('mcpServiceDirectory.disable')}
            </Button>
          ) : (
            <Button type="link" size="small" onClick={() => handleEnable(record)}>
              {t('mcpServiceDirectory.enable')}
            </Button>
          )}
          {record.is_published ? (
            <Button type="link" size="small" danger onClick={() => handleUnpublish(record)}>
              {t('mcpServiceDirectory.unpublish')}
            </Button>
          ) : (
            <Button type="link" size="small" onClick={() => handlePublish(record)}>
              {t('mcpServiceDirectory.publish')}
            </Button>
          )}
          <Button
            type="link"
            size="small"
            disabled={!record.is_published}
            onClick={() => testCallRef.current?.open(record)}
          >
            {t('mcpServiceDirectory.testCall')}
          </Button>
          <Button
            type="link"
            size="small"
            disabled={!record.is_published}
            onClick={() => keysDrawerRef.current?.open(record)}
          >
            {t('mcpServiceDirectory.authorizedKeys')}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div>
      {/* 筛选工具栏 */}
      <div
        className="yx-toolbar"
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 12,
          flexWrap: 'wrap',
          marginBottom: 16,
        }}
      >
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <Input.Search
            placeholder={t('mcpServiceDirectory.search')}
            allowClear
            style={{ width: 260 }}
            onSearch={(v) => setSearch(v.trim())}
            onChange={(e) => {
              if (!e.target.value) setSearch('');
            }}
          />
          <Select
            placeholder={t('mcpServiceDirectory.filterSpace')}
            value={spaceId || undefined}
            onChange={(v) => setSpaceId(v || '')}
            allowClear
            style={{ width: 160 }}
            options={spaceOptions}
          />
          <Select
            value={status}
            onChange={setStatus}
            style={{ width: 140 }}
            options={[
              { value: '', label: t('mcpServiceDirectory.statusAll') },
              { value: 'published', label: t('mcpServiceDirectory.statusPublished') },
              { value: 'unpublished', label: t('mcpServiceDirectory.statusUnpublished') },
            ]}
          />
        </div>
        <Button icon={<SyncOutlined />} loading={syncing} onClick={handleSync}>
          {t('mcpServiceDirectory.sync')}
        </Button>
      </div>

      <Table
        rowKey="id"
        columns={columns}
        dataSource={items}
        pagination={{ pageSize: 10 }}
        scroll={{ x: 'max-content' }}
        bordered
        loading={loading}
        locale={{
          emptyText: hasFilter ? (
            <Empty description={t('mcpServiceDirectory.emptySearchText')} />
          ) : (
            <Empty description={t('mcpServiceDirectory.emptyText')} />
          ),
        }}
      />

      <TestCallModal ref={testCallRef} />
      <AuthorizedKeysDrawer ref={keysDrawerRef} />
      <McpServiceDetailDrawer ref={detailDrawerRef} />
    </div>
  );
}
