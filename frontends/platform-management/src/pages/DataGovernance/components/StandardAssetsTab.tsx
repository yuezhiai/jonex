import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  Input,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '@jonex/shared-lib';
import { getShellContext } from '@jonex/shell-sdk';
import { listStandardAssets } from '@/api/standardAssets';
import type {
  DgStandardAssetItem,
  DgStandardAssetKind,
  DgStandardAssetStatus,
} from '../types';
import { KINDS, STATUS_COLORS, kindLabelKey } from '../standardAssetUtils';
import { DG_PAGE_SIZE, DG_PAGE_SIZE_OPTIONS } from '../constant';
import { loadErrorText, pageAfterSizeChange } from '../standardUtils';
import StandardAssetDrawer from './StandardAssetDrawer';
import type { StandardAssetDrawerMode } from './StandardAssetDrawer';

const STATUSES: DgStandardAssetStatus[] = ['draft', 'published', 'retired'];

/**
 * 标准资产列表（术语 / 数据元 / 指标口径卡）。
 *
 * - 仅租户管理员能带 `includeDrafts` 看到草稿，写按钮也只在管理员下渲染；
 * - 所有请求带序号，切 kind / 筛选 / 翻页 / 关抽屉后丢弃旧响应；
 * - 草稿标记只认列表行的 `hasDraft`（详情接口不可靠）。
 */
export default function StandardAssetsTab(): JSX.Element {
  const { t } = useTranslation();
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const isTenantAdmin = getShellContext()?.getCurrentUser?.()?.isTenantAdmin === true;

  const [kind, setKind] = useState<DgStandardAssetKind>('term');
  const [status, setStatus] = useState<DgStandardAssetStatus | ''>('');
  const [keyword, setKeyword] = useState('');
  const [appliedKeyword, setAppliedKeyword] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DG_PAGE_SIZE);
  const [rows, setRows] = useState<DgStandardAssetItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<StandardAssetDrawerMode | null>(null);

  const seq = useRef(0);

  const load = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await listStandardAssets({
        kind,
        status: status || undefined,
        keyword: appliedKeyword || undefined,
        includeDrafts: isTenantAdmin ? true : undefined,
        page,
        pageSize,
      });
      if (my !== seq.current) return;
      setRows(result.items);
      setTotal(result.total);
    } catch (err: unknown) {
      if (my !== seq.current) return;
      setRows([]);
      setTotal(0);
      setError(
        loadErrorText(err, 'dataGovernance.standardAssets.loadFailed', tRef.current),
      );
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [kind, status, appliedKeyword, page, pageSize, isTenantAdmin]);

  useEffect(() => {
    void load();
  }, [load]);

  // 列表刷新后同步抽屉里的列表行：草稿标记/状态以最新行数据为准。
  useEffect(() => {
    setDrawer((prev) => {
      if (prev?.type !== 'view') return prev;
      const next = rows.find((row) => row.assetId === prev.row.assetId);
      return next && next !== prev.row ? { type: 'view', row: next } : prev;
    });
  }, [rows]);

  const applySearch = () => {
    const nextKeyword = keyword.trim();
    if (page === 1 && nextKeyword === appliedKeyword) {
      // 条件未变：显式刷新，避免 setState 相同值被 React 跳过。
      void load();
      return;
    }
    setPage(1);
    setAppliedKeyword(nextKeyword);
  };

  const changeKind = (value: DgStandardAssetKind) => {
    setKind(value);
    setPage(1);
  };

  const changeStatus = (value: DgStandardAssetStatus | '') => {
    setStatus(value);
    setPage(1);
  };

  const columns: ColumnsType<DgStandardAssetItem> = [
    {
      title: t('dataGovernance.standardAssets.code'),
      dataIndex: 'code',
      width: 180,
      ellipsis: true,
    },
    {
      title: t('dataGovernance.standardAssets.name'),
      dataIndex: 'name',
      width: 220,
      ellipsis: true,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      width: 120,
      render: (value: DgStandardAssetStatus) => (
        <Tag color={STATUS_COLORS[value]}>
          {t(`dataGovernance.standardAssets.status.${value}`)}
        </Tag>
      ),
    },
    {
      title: t('dataGovernance.standardAssets.version'),
      key: 'version',
      width: 110,
      // 代表行版本：有 published 时代表行就是 published，不再回填草稿版本。
      render: (_: unknown, row) => `v${row.version}`,
    },
    {
      title: t('dataGovernance.standardAssets.draft'),
      dataIndex: 'hasDraft',
      width: 140,
      render: (value: boolean, row) =>
        // 草稿行本身不再重复标「有草稿」。
        value && row.status !== 'draft' ? (
          <Tag color="gold">{t('dataGovernance.standardAssets.draft')}</Tag>
        ) : (
          '—'
        ),
    },
    {
      title: t('dataGovernance.standardAssets.updatedAt'),
      dataIndex: 'updatedAt',
      width: 170,
      render: (value: string | null) => (value ? formatDateTime(value) : '—'),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 100,
      render: (_: unknown, row) => (
        <a onClick={() => setDrawer({ type: 'view', row })}>
          {t('dataGovernance.standardAssets.view')}
        </a>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <Segmented
          value={kind}
          onChange={(value) => changeKind(value as DgStandardAssetKind)}
          options={KINDS.map((value) => ({ value, label: t(kindLabelKey(value)) }))}
        />
        <Select
          value={status}
          aria-label={t('dataGovernance.standardAssets.statusFilter')}
          style={{ width: 160 }}
          onChange={(value: DgStandardAssetStatus | '') => changeStatus(value)}
          options={[
            { value: '', label: t('dataGovernance.standardAssets.allStatus') },
            // 草稿只有管理员能看到（includeDrafts），非管理员不提供该筛选项。
            ...STATUSES.filter((value) => isTenantAdmin || value !== 'draft').map(
              (value) => ({
                value,
                label: t(`dataGovernance.standardAssets.status.${value}`),
              }),
            ),
          ]}
        />
        <Input
          allowClear
          value={keyword}
          placeholder={t('dataGovernance.standardAssets.keyword')}
          onChange={(event) => setKeyword(event.target.value)}
          onPressEnter={applySearch}
          style={{ width: 240 }}
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={applySearch}>
          {t('dataGovernance.filter.search')}
        </Button>
        <Tooltip title={t('common.refresh')}>
          <Button
            aria-label={t('common.refresh')}
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void load()}
          />
        </Tooltip>
        {isTenantAdmin && (
          <Button
            icon={<PlusOutlined />}
            onClick={() => setDrawer({ type: 'create', kind })}
          >
            {t('dataGovernance.standardAssets.create')}
          </Button>
        )}
      </Space>

      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => void load()}
            >
              {t('common.retry')}
            </Button>
          }
        />
      ) : (
        <Table<DgStandardAssetItem>
          rowKey="assetId"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1040 }}
          onRow={(record) => ({
            onClick: () => setDrawer({ type: 'view', row: record }),
            style: { cursor: 'pointer' },
          })}
          locale={{
            emptyText: <Empty description={t('dataGovernance.standardAssets.empty')} />,
          }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: DG_PAGE_SIZE_OPTIONS,
            onChange: (nextPage, nextSize) => {
              // 改每页条数时回到第 1 页，避免旧页码超出新页数后落到空页。
              setPage(pageAfterSizeChange(pageSize, nextPage, nextSize));
              setPageSize(nextSize);
            },
            showTotal: (n) => t('dataGovernance.total', { total: n }),
          }}
        />
      )}

      <StandardAssetDrawer
        open={Boolean(drawer)}
        mode={drawer ?? { type: 'create', kind }}
        onClose={() => setDrawer(null)}
        onChanged={() => void load()}
        onSelectRow={(row) => setDrawer({ type: 'view', row })}
      />
    </Space>
  );
}
