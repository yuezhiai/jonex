// 工具目录管理（仅平台管理员可见）：列出全部状态的工具，新建 / 编辑 / 上下架。
// 目录是平台共享的，改动对所有租户生效；租户启用 / 停用仍在「租户启用」页签。
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Button,
  Empty,
  Input,
  Popconfirm,
  Result,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { EditOutlined, PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { formatDateTime } from '@jonex/shared-lib';
import {
  createCatalogTool,
  fetchCatalog,
  updateCatalogTool,
  type CatalogItem,
  type CatalogStatus,
  type SaveCatalogPayload,
} from '../../api/skills';
import { CATEGORY_LABEL_KEYS } from './shared';
import CatalogFormModal from './CatalogFormModal';

const { Text } = Typography;
const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;

interface CatalogAdminProps {
  /** 目录有改动时通知外层刷新租户视图 */
  onChanged?: () => void;
}

export default function CatalogAdmin({ onChanged }: CatalogAdminProps) {
  const { t } = useTranslation();
  const [items, setItems] = useState<CatalogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [debouncedKeyword, setDebouncedKeyword] = useState('');
  const [status, setStatus] = useState<CatalogStatus | undefined>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<CatalogItem | null>(null);
  /** 请求序号：快速切换筛选时丢弃过期响应 */
  const requestSeq = useRef(0);

  const load = useCallback(
    async (targetPage: number) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      setError(null);
      try {
        const result = await fetchCatalog({
          offset: (targetPage - 1) * PAGE_SIZE,
          limit: PAGE_SIZE,
          status,
          keyword: debouncedKeyword || undefined,
        });
        if (seq !== requestSeq.current) return;
        setItems(result.items);
        setTotal(result.total);
        setPage(targetPage);
      } catch (err: unknown) {
        if (seq !== requestSeq.current) return;
        setError(err instanceof Error ? err.message : t('skills.catalog.loadFailed'));
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [status, debouncedKeyword, t],
  );

  // 状态 / 关键词变化都回到第一页重新取数
  useEffect(() => {
    load(1);
  }, [load]);

  useEffect(() => {
    const next = keyword.trim();
    if (next === debouncedKeyword) return;
    const timer = setTimeout(() => setDebouncedKeyword(next), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [keyword, debouncedKeyword]);

  const hasFilters = !!debouncedKeyword || !!status;

  function clearFilters() {
    setKeyword('');
    setDebouncedKeyword('');
    setStatus(undefined);
  }

  function openCreate() {
    setEditing(null);
    setModalOpen(true);
  }

  async function handleToggle(item: CatalogItem) {
    const next: CatalogStatus = item.status === 'published' ? 'disabled' : 'published';
    setTogglingId(item.id);
    try {
      await updateCatalogTool(item.id, { status: next });
      message.success(next === 'published' ? t('skills.catalog.published') : t('skills.catalog.unpublished'));
      await load(page);
      onChanged?.();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.operationFailed'));
    } finally {
      setTogglingId(null);
    }
  }

  async function handleSubmit(data: SaveCatalogPayload) {
    if (editing) {
      await updateCatalogTool(editing.id, data);
    } else {
      await createCatalogTool(data);
    }
    message.success(t('common.saveSuccess'));
    await load(editing ? page : 1);
    onChanged?.();
  }

  function renderRuntime(item: CatalogItem) {
    const runtime = item.runtime;
    if (!runtime) {
      return (
        <Tooltip title={t('skills.catalog.runtimeNoneHint')}>
          <Tag>{t('skills.catalog.runtimeNone')}</Tag>
        </Tooltip>
      );
    }
    if (runtime.type === 'echo') return <Tag color="purple">echo</Tag>;
    return (
      <Space size={4} direction="vertical" style={{ maxWidth: 150 }}>
        <Space size={4} wrap>
          <Tag color="geekblue">{runtime.method}</Tag>
          {runtime.auth && runtime.auth.type !== 'none' && (
            <Tag color={item.has_secret ? 'green' : 'orange'}>
              {item.has_secret ? t('skills.catalog.authReady') : t('skills.catalog.authMissing')}
            </Tag>
          )}
        </Space>
        <Text type="secondary" ellipsis={{ tooltip: runtime.url }} style={{ fontSize: 12, maxWidth: 150 }}>
          {runtime.url}
        </Text>
      </Space>
    );
  }

  const columns: ColumnsType<CatalogItem> = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      width: 220,
      render: (_v, item) => (
        <Space direction="vertical" size={0}>
          <Text strong>{item.name}</Text>
          <Text type="secondary" ellipsis={{ tooltip: item.description }} style={{ fontSize: 12, maxWidth: 260 }}>
            {item.description || t('common.noDescription')}
          </Text>
        </Space>
      ),
    },
    {
      title: t('skills.toolName'),
      dataIndex: 'tool_name',
      width: 250,
      render: (v: string) => (
        <Text code copyable={{ text: v }} style={{ whiteSpace: 'nowrap' }}>
          {v}
        </Text>
      ),
    },
    {
      title: t('skills.categoryLabel'),
      dataIndex: 'category',
      width: 90,
      render: (v: string) => (
        <Tag className="skill-tag-category">{CATEGORY_LABEL_KEYS[v] ? t(CATEGORY_LABEL_KEYS[v]) : v}</Tag>
      ),
    },
    {
      title: t('skills.catalog.runtime'),
      dataIndex: 'runtime',
      width: 150,
      render: (_v, item) => renderRuntime(item),
    },
    {
      title: t('skills.catalog.artifact'),
      dataIndex: 'has_artifact',
      width: 70,
      render: (v: boolean) => (v ? <Tag color="gold">MCP</Tag> : <Text type="secondary">-</Text>),
    },
    {
      title: t('skills.catalog.publishStatus'),
      dataIndex: 'status',
      width: 120,
      render: (_v, item) => (
        <Space size={6}>
          <Popconfirm
            title={
              item.status === 'published' ? t('skills.catalog.confirmUnpublish') : t('skills.catalog.confirmPublish')
            }
            onConfirm={() => handleToggle(item)}
            okText={t('common.confirm')}
            cancelText={t('common.cancel')}
          >
            <Switch size="small" checked={item.status === 'published'} loading={togglingId === item.id} />
          </Popconfirm>
          <Text type={item.status === 'published' ? undefined : 'secondary'} style={{ fontSize: 12 }}>
            {item.status === 'published' ? t('skills.catalog.statusPublished') : t('skills.catalog.statusDisabled')}
          </Text>
        </Space>
      ),
    },
    {
      title: t('skills.catalog.updatedAt'),
      dataIndex: 'updated_at',
      width: 140,
      render: (v: string | null | undefined) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {v ? formatDateTime(v) : '-'}
        </Text>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 110,
      fixed: 'right',
      render: (_v, item) => (
        <Button
          type="link"
          size="small"
          icon={<EditOutlined />}
          onClick={() => {
            setEditing(item);
            setModalOpen(true);
          }}
        >
          {t('common.edit')}
        </Button>
      ),
    },
  ];

  return (
    <>
      <p className="catalog-subtitle">{t('skills.catalog.subtitle')}</p>

      <div className="catalog-toolbar">
        <Input
          placeholder={t('skills.searchPlaceholder')}
          prefix={<SearchOutlined />}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          allowClear
          style={{ width: 280 }}
        />
        <Select
          placeholder={t('skills.catalog.allStatuses')}
          value={status}
          onChange={(v) => setStatus(v)}
          allowClear
          style={{ width: 140 }}
          options={[
            { label: t('skills.catalog.statusPublished'), value: 'published' },
            { label: t('skills.catalog.statusDisabled'), value: 'disabled' },
          ]}
        />
        <Tooltip title={t('common.refresh')}>
          <Button icon={<ReloadOutlined />} aria-label={t('common.refresh')} onClick={() => load(page)} />
        </Tooltip>
        <Button type="primary" icon={<PlusOutlined />} className="catalog-toolbar-create" onClick={openCreate}>
          {t('skills.catalog.createBtn')}
        </Button>
      </div>

      {error ? (
        <Result
          status="error"
          title={t('common.loadFailed')}
          subTitle={error}
          extra={
            <Button type="primary" onClick={() => load(page)}>
              {t('common.retry')}
            </Button>
          }
        />
      ) : (
        <Table<CatalogItem>
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={loading}
          scroll={{ x: 1080 }}
          locale={{
            emptyText: (
              <Empty description={hasFilters ? t('skills.emptyText') : t('skills.catalog.emptyText')}>
                {hasFilters ? (
                  <Button onClick={clearFilters}>{t('common.clearFilters')}</Button>
                ) : (
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                    {t('skills.catalog.createBtn')}
                  </Button>
                )}
              </Empty>
            ),
          }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            showSizeChanger: false,
            onChange: (p) => load(p),
            showTotal: (n) => t('common.totalPage', { total: n }),
          }}
        />
      )}

      <CatalogFormModal
        open={modalOpen}
        item={editing}
        onClose={() => {
          setModalOpen(false);
          setEditing(null);
        }}
        onSubmit={handleSubmit}
      />
    </>
  );
}
