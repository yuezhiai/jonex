import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Table, Tag, Input, Select, Button, Tooltip, Space, Empty, Result } from 'antd';
import { SyncOutlined, ReloadOutlined } from '@ant-design/icons';
import type { TableProps } from 'antd';
import { listMcpServices, syncMcpServices } from '@/api/mcpServices';
import { listSpaces } from '@/api/spaces';
import type { McpServiceItem } from '@/api/mcpServices';
import { safeMessage } from '@/utils/safeMessage';
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

interface McpServicesTabProps {
  /** 点击「前往 MCP Key 管理」时切换到服务访问Key 视图 */
  onGoToKeys?: () => void;
}

/** MCP 服务目录 — 服务列表 Tab */
export default function McpServicesTab({ onGoToKeys }: McpServicesTabProps) {
  const { t } = useTranslation();
  const [items, setItems] = useState<McpServiceItem[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 筛选条件
  const [search, setSearch] = useState('');
  // 搜索输入框原始值（防抖后才同步到查询条件 search）
  const [searchInput, setSearchInput] = useState('');
  const [spaceId, setSpaceId] = useState('');
  // 领域空间筛选选项（来源 GET /knowledge-base/spaces 接口，而非列表数据聚合）
  const [spaceOptions, setSpaceOptions] = useState<{ label: string; value: string }[]>([]);

  const detailDrawerRef = useRef<McpServiceDetailDrawerHandle>(null);

  const hasFilter = Boolean(search || spaceId);

  /** 加载列表（携带当前筛选条件） */
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listMcpServices({
        search: search || undefined,
        space_id: spaceId || undefined,
      });
      setItems(result.items);
    } catch (err: unknown) {
      // 接口报错：渲染内联 Result 错误态 + 重试按钮（与 McpKeysTab 一致）
      setError(err instanceof Error ? err.message : t('mcpServiceDirectory.loadFailed'));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [search, spaceId]);

  useEffect(() => {
    load();
  }, [load]);

  /** 搜索输入防抖：输入停止 300ms 后才同步到查询条件，触发重新查询 */
  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchInput.trim()), 300);
    return () => clearTimeout(timer);
  }, [searchInput]);

  /** 领域空间筛选选项（来源接口 GET /knowledge-base/spaces，失败静默为空） */
  useEffect(() => {
    listSpaces()
      .then((list) => setSpaceOptions(list.map((s) => ({ label: s.name ?? s.id, value: s.id }))))
      .catch(() => {
        // 失败静默，下拉仅剩「全部领域空间」
      });
  }, []);

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

  const columns: TableProps<McpServiceItem>['columns'] = [
    {
      title: t('mcpServiceDirectory.colName'),
      dataIndex: 'name',
      key: 'name',
      render: (name: string, record: McpServiceItem) => (
        <Space size={6}>
          <Button
            type="link"
            style={{ padding: 0, height: 'auto', color: '#1C6FEA', fontWeight: 600 }}
            onClick={() => detailDrawerRef.current?.open(record)}
          >
            {name}
          </Button>
          {record.service_type === 'system' && (
            <Tag color="blue">{t('mcpServiceDirectory.systemService')}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t('mcpServiceDirectory.colToolName'),
      key: 'tool_name',
      render: (_: unknown, record: McpServiceItem) => {
        // 领域服务走 tool_name，系统服务内置条目走 tool（固定不可改）
        const tool = record.tool_name || record.tool;
        return tool ? <span>{tool}</span> : <span style={{ color: '#94a3b8' }}>-</span>;
      },
    },
    {
      title: t('mcpServiceDirectory.colSpace'),
      dataIndex: 'space_name',
      key: 'space_name',
      render: (v: string) => v || '-',
    },
    {
      title: t('mcpServiceDirectory.colKb'),
      dataIndex: 'kb_names',
      key: 'kb_names',
      render: (_: unknown, record: McpServiceItem) => {
        const names = record.kb_names || [];
        if (names.length === 0) return <span style={{ color: '#94a3b8' }}>-</span>;
        const shown = names.slice(0, 3);
        const rest = names.slice(3);
        return (
          <Space size={8} wrap>
            {shown.map((n) => (
              <Tag key={n}>{n}</Tag>
            ))}
            {rest.length > 0 && (
              <Tooltip title={rest.join('、')}>
                <Tag>+{rest.length}</Tag>
              </Tooltip>
            )}
          </Space>
        );
      },
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
      title: t('mcpServiceDirectory.colLastCall'),
      dataIndex: 'last_call_at',
      key: 'last_call_at',
      render: (v: string | null) => formatDate(v),
    },
  ];

  // ── 错误态覆盖：接口失败时渲染 Result + 重试按钮 ──
  if (error) {
    return (
      <Result
        status="error"
        title={t('mcpServiceDirectory.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" icon={<ReloadOutlined />} onClick={load}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

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
          <Input
            placeholder={t('mcpServiceDirectory.searchServicePlaceholder')}
            allowClear
            style={{ width: 280 }}
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
          <Select
            placeholder={t('mcpServiceDirectory.allSpaces')}
            value={spaceId || undefined}
            onChange={(v) => setSpaceId(v || '')}
            allowClear
            style={{ width: 160 }}
            options={spaceOptions}
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

      <McpServiceDetailDrawer ref={detailDrawerRef} onGoToKeys={onGoToKeys} />
    </div>
  );
}
