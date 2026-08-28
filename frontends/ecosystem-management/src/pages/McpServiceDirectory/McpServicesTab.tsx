import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Table, Tag, Input, Select, Button, Tooltip, Space, Empty, Result } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { TableProps } from 'antd';
import { listMcpServices } from '@/api/mcpServices';
import { listSpaces } from '@/api/spaces';
import type { McpServiceItem } from '@/api/mcpServices';
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 接口 HTTP status（403=权限不足时错误态换用权限图标，而非报错图标） */
  const [errorStatus, setErrorStatus] = useState<number | undefined>(undefined);

  // 筛选条件
  const [search, setSearch] = useState('');
  // 搜索输入框原始值（防抖后才同步到查询条件 search）
  const [searchInput, setSearchInput] = useState('');
  const [spaceId, setSpaceId] = useState('');
  // 领域空间筛选选项（来源 GET /knowledge-base/spaces 接口，而非列表数据聚合）
  const [spaceOptions, setSpaceOptions] = useState<{ label: string; value: string }[]>([]);

  const detailDrawerRef = useRef<McpServiceDetailDrawerHandle>(null);

  const hasFilter = Boolean(search || spaceId);

  /** 客户端筛选：按搜索词（名称 / Tool / 空间名）与领域空间过滤 */
  const filtered = useMemo(() => {
    const q = search.trim().toLocaleLowerCase();
    return items.filter((item) => {
      if (
        q &&
        !`${item.name || ''} ${item.tool || ''} ${item.space_name || ''}`.toLocaleLowerCase().includes(q)
      ) {
        return false;
      }
      if (spaceId && item.space_id !== spaceId) return false;
      return true;
    });
  }, [items, search, spaceId]);

  /** 加载列表（一次拉全量，筛选在客户端完成） */
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listMcpServices();
      setItems(result.items);
    } catch (err: unknown) {
      // 接口报错：渲染内联 Result 错误态 + 重试按钮（与 McpKeysTab 一致）
      // 权限不足(403)时错误态换用权限图标而非报错图标
      setError(err instanceof Error ? err.message : t('mcpServiceDirectory.loadFailed'));
      setErrorStatus(typeof (err as { status?: number } | null)?.status === 'number' ? (err as { status?: number }).status : undefined);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /** 搜索输入防抖：输入停止 300ms 后才同步到查询条件，触发客户端过滤 */
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
        </Space>
      ),
    },
    {
      title: t('mcpServiceDirectory.colCapabilityType'),
      key: 'capability_type',
      width: 100,
      render: (_: unknown, record: McpServiceItem) =>
        record.service_type === 'system' ? (
          <Tag color="blue">{t('mcpServiceDirectory.capabilityWrite')}</Tag>
        ) : (
          <Tag>{t('mcpServiceDirectory.capabilityDomain')}</Tag>
        ),
    },
    {
      title: t('mcpServiceDirectory.colToolName'),
      key: 'tool_name',
      render: (_: unknown, record: McpServiceItem) =>
        record.tool ? <span>{record.tool}</span> : <span style={{ color: '#94a3b8' }}>-</span>,
    },
    {
      title: t('mcpServiceDirectory.colSource'),
      key: 'source',
      width: 120,
      render: (_: unknown, record: McpServiceItem) =>
        record.service_type === 'system' ? (
          <span>{t('mcpServiceDirectory.sourceBuiltin')}</span>
        ) : (
          record.space_name || '-'
        ),
    },
    {
      title: t('mcpServiceDirectory.colResourceScope'),
      key: 'resource_scope',
      render: (_: unknown, record: McpServiceItem) => {
        // 知识写入（system）无预授权知识库，资源范围由 Key 授权决定
        if (record.service_type === 'system') {
          return <span style={{ color: '#94a3b8' }}>{t('mcpServiceDirectory.scopeByKey')}</span>;
        }
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
      render: (v: boolean, record: McpServiceItem) =>
        v ? (
          <Space size={4} wrap>
            <Tag color="success">{t('mcpServiceDirectory.statusPublished')}</Tag>
            {record.enabled === 0 && (
              <Tooltip title={t('mcpServiceDirectory.statusUnavailableTip')}>
                <Tag color="orange">{t('mcpServiceDirectory.statusUnavailable')}</Tag>
              </Tooltip>
            )}
          </Space>
        ) : (
          <Tag>{t('mcpServiceDirectory.statusUnpublished')}</Tag>
        ),
    },
    {
      title: t('mcpServiceDirectory.colLastCall'),
      dataIndex: 'last_call_at',
      key: 'last_call_at',
      width: 150,
      render: (v: string | null) =>
        v ? formatDate(v) : <span style={{ color: '#94a3b8' }}>{t('mcpServiceDirectory.neverCalled')}</span>,
    },
    {
      title: t('mcpServiceDirectory.colActions'),
      key: 'actions',
      width: 90,
      render: (_: unknown, record: McpServiceItem) => (
        <Button type="link" size="small" onClick={() => detailDrawerRef.current?.open(record)}>
          {t('mcpServiceDirectory.viewDetail')}
        </Button>
      ),
    },
  ];

  // ── 错误态覆盖：接口失败时渲染 Result + 重试按钮 ──
  // 权限不足(403)时使用 antd 内置 403 权限图标，而非报错图标
  if (error) {
    const forbidden = errorStatus === 403;
    return (
      <Result
        status={forbidden ? '403' : 'error'}
        title={forbidden ? t('mcpServiceDirectory.forbiddenTitle') : t('mcpServiceDirectory.loadFailed')}
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
      </div>

      <Table
        rowKey="id"
        columns={columns}
        dataSource={filtered}
        pagination={{ pageSize: 10 }}
        scroll={{ x: 'max-content' }}
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
