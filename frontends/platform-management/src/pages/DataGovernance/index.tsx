import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ReloadOutlined,
  SafetyOutlined,
  SearchOutlined,
  SettingOutlined,
  TagsOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getShellContext } from '@jonex/shell-sdk';
import { formatDateTime, PageContent, PageHeader, usePermission } from '@jonex/shared-lib';
import { listKnowledgeBaseSpaces } from '@/api/knowledgeBase';
import { listKbOptions } from '@/api/dataOperations';
import { listAssets, listClassificationConfig } from '@/api/dataGovernance';
import type {
  DgAssetItem,
  DgAssetListParams,
  DgAssetType,
  DgAssetTypeFilter,
  DgClassificationConfig,
} from './types';
import {
  DG_ASSET_TYPES,
  DG_DATA_SOURCE_STATUSES,
  DG_DOCUMENT_STATUSES,
  DG_PAGE_SIZE,
  DG_PAGE_SIZE_OPTIONS,
  DG_PERMISSION_READ,
  DG_SELECTION_COLUMN_WIDTH,
} from './constant';
import {
  buildCategoryOptions,
  buildLevelOptions,
  errorMeta,
  levelColor,
  levelLabel,
  qualityColor,
  qualityLabelKey,
  statusColor,
  statusLabelKey,
} from './utils';
import AssetDrawer from './drawers/AssetDrawer';
import ClassificationBatchModal from './components/ClassificationBatchModal';
import ClassificationConfigModal from './components/ClassificationConfigModal';
import RetentionPolicyModal from './components/RetentionPolicyModal';
import StandardCatalogTab from './components/StandardCatalogTab';
import './index.scss';

interface Filters {
  assetType: DgAssetTypeFilter;
  spaceId?: string;
  kbId?: string;
  domain?: string;
  owner?: string;
  sensitivity?: DgAssetListParams['sensitivity'];
  category?: string;
  status?: string;
  keyword?: string;
}

const INITIAL_FILTERS: Filters = { assetType: 'all' };

export default function DataGovernancePage() {
  const { t } = useTranslation();
  const { hasPerm } = usePermission();
  const canRead = hasPerm(DG_PERMISSION_READ);
  const isTenantAdmin = getShellContext()?.getCurrentUser?.()?.isTenantAdmin === true;

  const [filters, setFilters] = useState<Filters>(INITIAL_FILTERS);
  const [applied, setApplied] = useState<Filters>(INITIAL_FILTERS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DG_PAGE_SIZE);
  const [items, setItems] = useState<DgAssetItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<{ type: DgAssetType; id: string } | null>(null);
  const [policyOpen, setPolicyOpen] = useState(false);

  const [spaces, setSpaces] = useState<Array<{ id: string; name: string }>>([]);
  const [kbs, setKbs] = useState<Array<{ id: string; name: string }>>([]);

  // 第 4 期分级分类：词表配置、配置/批量弹窗与勾选行。
  const [classificationConfig, setClassificationConfig] = useState<DgClassificationConfig>({
    levels: [],
    categories: [],
  });
  const [configOpen, setConfigOpen] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [selectedRows, setSelectedRows] = useState<DgAssetItem[]>([]);
  const [configKbs, setConfigKbs] = useState<Array<{ id: string; name: string }>>([]);
  const [classificationConfigLoading, setClassificationConfigLoading] = useState(false);
  const [classificationConfigError, setClassificationConfigError] = useState<string | null>(
    null,
  );

  // 请求竞态：只有最后一次发出的请求能落地（筛选/翻页连点时丢弃过期响应）。
  const requestSeq = useRef(0);
  const kbSeq = useRef(0);
  const configSeq = useRef(0);

  const loadClassificationConfig = useCallback(async () => {
    if (!canRead) {
      configSeq.current += 1;
      setClassificationConfig({ levels: [], categories: [] });
      setClassificationConfigLoading(false);
      setClassificationConfigError(null);
      return;
    }
    const seq = ++configSeq.current;
    setClassificationConfigLoading(true);
    setClassificationConfigError(null);
    try {
      const next = await listClassificationConfig();
      if (seq !== configSeq.current) return;
      setClassificationConfig(next);
    } catch (err: unknown) {
      if (seq !== configSeq.current) return;
      // 失败时保留上一次成功加载的词表（首次失败时当前值本就为空），
      // 同时给出可重试的本地化错误，避免页面静默退化成「暂无数据」。
      const meta = errorMeta(err);
      setClassificationConfigError(
        meta.status !== undefined && meta.message
          ? meta.message
          : t('dataGovernance.classification.configLoadFailed'),
      );
    } finally {
      if (seq === configSeq.current) setClassificationConfigLoading(false);
    }
  }, [canRead, t]);

  useEffect(() => {
    void loadClassificationConfig();
  }, [loadClassificationConfig]);

  // 配置弹窗的 KB 下拉独立于筛选：不传 spaceId，始终展示租户全量 KB。
  const refreshConfigKbs = useCallback(async () => {
    try {
      setConfigKbs(await listKbOptions());
    } catch {
      setConfigKbs([]);
    }
  }, []);

  // 入口可见性不能依赖被空间筛选后的 `kbs`：挂载时（有读权限后）先取一份全量 KB。
  useEffect(() => {
    if (!canRead) {
      setConfigKbs([]);
      return;
    }
    void refreshConfigKbs();
  }, [canRead, refreshConfigKbs]);

  const load = useCallback(async () => {
    // 列表一刷新（翻页/筛选/重试），旧的勾选可能已不在当前页，统一清空。
    setSelectedRows([]);
    // 无权限时不发请求：effects 已在渲染后无条件挂载，必须在这里短路；
    // 同时推进序号，保证已飞行的请求也不会再落地。
    if (!canRead) {
      requestSeq.current += 1;
      kbSeq.current += 1;
      setItems([]);
      setTotal(0);
      setError(null);
      setLoading(false);
      return;
    }
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const params: DgAssetListParams = {
        assetType: applied.assetType,
        spaceId: applied.spaceId,
        kbId: applied.kbId,
        domain: applied.domain,
        owner: applied.owner,
        sensitivity: applied.sensitivity,
        category: applied.category,
        status: applied.status,
        keyword: applied.keyword,
        page,
        pageSize,
      };
      const result = await listAssets(params);
      if (seq !== requestSeq.current) return;
      setItems(result.items);
      setTotal(result.total);
    } catch (err: unknown) {
      if (seq !== requestSeq.current) return;
      setItems([]);
      setTotal(0);
      setError(err instanceof Error && err.message ? err.message : t('dataGovernance.error.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [applied, page, pageSize, t, canRead]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!canRead) {
      setSpaces([]);
      return;
    }
    listKnowledgeBaseSpaces()
      .then(setSpaces)
      .catch(() => setSpaces([]));
  }, [canRead]);

  useEffect(() => {
    if (!canRead) {
      kbSeq.current += 1;
      setKbs([]);
      return;
    }
    // 空间快速切换时，旧空间的 KB 列表可能后到，用序号丢弃过期响应。
    const seq = ++kbSeq.current;
    listKbOptions(filters.spaceId)
      .then((next) => {
        if (seq === kbSeq.current) setKbs(next);
      })
      .catch(() => {
        if (seq === kbSeq.current) setKbs([]);
      });
  }, [filters.spaceId, canRead]);

  // 打开即并行刷新词表与 KB 列表；弹窗内部按 loading/error 呈现，不阻塞打开。
  const openConfig = useCallback(() => {
    setConfigOpen(true);
    void loadClassificationConfig();
    void refreshConfigKbs();
  }, [loadClassificationConfig, refreshConfigKbs]);

  const applyFilters = useCallback(() => {
    setPage(1);
    // 显式新建对象：即使值未变也强制触发重新取数（引用相同会被 React 跳过）。
    setApplied({ ...filters });
  }, [filters]);

  const resetFilters = useCallback(() => {
    setFilters({ ...INITIAL_FILTERS });
    setPage(1);
    setApplied({ ...INITIAL_FILTERS });
  }, []);

  const statusOptions = useMemo<readonly string[]>(() => {
    if (filters.assetType === 'document') {
      // `deleted` 在目录查询里恒被 `is_deleted=0` 过滤掉，作为筛选值只会命中 0 条。
      return DG_DOCUMENT_STATUSES.filter((value) => value !== 'deleted');
    }
    if (filters.assetType === 'data_source') return DG_DATA_SOURCE_STATUSES;
    return [];
  }, [filters.assetType]);

  const hasActiveFilters =
    applied.assetType !== 'all' ||
    Boolean(
      applied.spaceId ||
        applied.kbId ||
        applied.domain ||
        applied.owner ||
        applied.sensitivity ||
        applied.category ||
        applied.status ||
        applied.keyword,
    );

  const levelName = (code: string | null) =>
    levelLabel(t, classificationConfig.levels.find((level) => level.code === code), code);

  const categoryName = (code: string) =>
    classificationConfig.categories.find((category) => category.code === code)?.name ?? code;

  const columns: ColumnsType<DgAssetItem> = [
    {
      title: t('dataGovernance.column.type'),
      dataIndex: 'assetType',
      width: 90,
      render: (value: DgAssetItem['assetType']) => (
        <Tag color={value === 'document' ? 'blue' : 'purple'}>
          {t(`dataGovernance.assetType.${value}`)}
        </Tag>
      ),
    },
    {
      title: t('dataGovernance.column.name'),
      dataIndex: 'name',
      width: 260,
      fixed: 'left',
      // 关闭表格自带的 title 提示，改用 Tooltip：窄窗口下仍能看到完整名称。
      ellipsis: { showTitle: false },
      render: (name: string, row) => (
        <Tooltip title={name} placement="topLeft">
          <Button
            type="link"
            style={{
              display: 'block',
              padding: 0,
              maxWidth: '100%',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              textAlign: 'left',
            }}
            onClick={() => setDrawer({ type: row.assetType, id: row.assetId })}
          >
            {name}
          </Button>
        </Tooltip>
      ),
    },
    { title: t('dataGovernance.column.kb'), dataIndex: 'kbName', width: 200, ellipsis: true },
    {
      title: t('dataGovernance.column.status'),
      dataIndex: 'status',
      width: 110,
      render: (status: string, row) => (
        <Tag color={statusColor(status)}>{t(statusLabelKey(row.assetType, status))}</Tag>
      ),
    },
    {
      title: t('dataGovernance.column.quality'),
      dataIndex: 'qualityVerdict',
      width: 110,
      render: (verdict: DgAssetItem['qualityVerdict'], row) => {
        if (row.assetType !== 'document') return <span>—</span>;
        // 配色统一走 qualityColor，避免列表与详情抽屉两处漂移。
        return <Tag color={qualityColor(verdict)}>{t(qualityLabelKey(verdict))}</Tag>;
      },
    },
    {
      title: t('dataGovernance.column.owner'),
      dataIndex: 'owner',
      width: 140,
      ellipsis: true,
      render: (owner: string | null) =>
        owner || <span className="yx-dg-muted">{t('dataGovernance.unset')}</span>,
    },
    {
      title: t('dataGovernance.column.domain'),
      dataIndex: 'domain',
      width: 140,
      ellipsis: true,
      render: (domain: string | null) =>
        domain || <span className="yx-dg-muted">{t('dataGovernance.unset')}</span>,
    },
    {
      title: t('dataGovernance.classification.level'),
      dataIndex: 'sensitivity',
      width: 110,
      render: (value: DgAssetItem['sensitivity']) => (
        <Tag color={levelColor(value, classificationConfig.levels)}>{levelName(value)}</Tag>
      ),
    },
    {
      title: t('dataGovernance.classification.categories'),
      dataIndex: 'classificationCategories',
      width: 180,
      render: (values: string[]) => {
        if (!values.length) return <span>—</span>;
        return (
          <Space size={[0, 4]} wrap>
            {values.map((code) => (
              <Tag key={code} color="blue">
                {categoryName(code)}
              </Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: t('dataGovernance.column.updatedAt'),
      dataIndex: 'updatedAt',
      width: 170,
      render: (value: string) => formatDateTime(value),
    },
  ];

  // 横向滚动宽度由列定义求和派生（单一来源），并计入 rowSelection 选择列：
  // 窗口窄于总和时才出现滚动条，名称列保持固定可见。
  const tableScrollX =
    columns.reduce(
      (sum, column) => sum + (typeof column.width === 'number' ? column.width : 0),
      0,
    ) + DG_SELECTION_COLUMN_WIDTH;

  if (!canRead) {
    return (
      <>
        <PageHeader title={t('navigation.dataGovernance')} />
        <PageContent>
          <Empty description={t('dataGovernance.error.forbidden')} />
        </PageContent>
      </>
    );
  }

  return (
    <>
      <PageHeader title={t('navigation.dataGovernance')} />
      <PageContent showCard={false}>
        <Tabs
          items={[
            {
              key: 'assets',
              label: t('dataGovernance.title'),
              children: (
                <div className="yx-dg-page">
          <div className="yx-dg-toolbar">
            <Select<Filters['assetType']>
              value={filters.assetType}
              options={[
                { value: 'all', label: t('dataGovernance.filter.all') },
                ...DG_ASSET_TYPES.map((v) => ({
                  value: v,
                  label: t(`dataGovernance.assetType.${v}`),
                })),
              ]}
              onChange={(assetType) =>
                setFilters((f) => ({ ...f, assetType, status: undefined }))
              }
              style={{ width: 120 }}
            />
            <Select
              allowClear
              placeholder={t('dataGovernance.filter.space')}
              value={filters.spaceId}
              options={spaces.map((s) => ({ value: s.id, label: s.name }))}
              onChange={(spaceId) => setFilters((f) => ({ ...f, spaceId, kbId: undefined }))}
              style={{ width: 180 }}
            />
            <Select
              allowClear
              placeholder={t('dataGovernance.filter.kb')}
              value={filters.kbId}
              options={kbs.map((k) => ({ value: k.id, label: k.name }))}
              onChange={(kbId) => setFilters((f) => ({ ...f, kbId }))}
              style={{ width: 200 }}
            />
            <Input
              allowClear
              placeholder={t('dataGovernance.filter.domain')}
              value={filters.domain}
              onChange={(e) => setFilters((f) => ({ ...f, domain: e.target.value }))}
              onPressEnter={applyFilters}
              style={{ width: 140 }}
            />
            <Input
              allowClear
              placeholder={t('dataGovernance.filter.owner')}
              value={filters.owner}
              onChange={(e) => setFilters((f) => ({ ...f, owner: e.target.value }))}
              onPressEnter={applyFilters}
              style={{ width: 140 }}
            />
            <Select<NonNullable<Filters['sensitivity']>>
              allowClear
              placeholder={t('dataGovernance.classification.level')}
              value={filters.sensitivity}
              options={[
                // 停用级别不 disabled：历史资产可能仍带着该级别，筛选取值必须可选。
                ...buildLevelOptions(t, classificationConfig.levels, {
                  disableInactive: false,
                }),
                {
                  value: 'unclassified',
                  label: t('dataGovernance.classification.unclassified'),
                },
              ]}
              onChange={(sensitivity) => setFilters((f) => ({ ...f, sensitivity }))}
              style={{ width: 140 }}
            />
            <Select
              allowClear
              placeholder={t('dataGovernance.classification.categories')}
              value={filters.category}
              options={buildCategoryOptions(classificationConfig.categories, t, {
                disableInactive: false,
              })}
              onChange={(category) => setFilters((f) => ({ ...f, category }))}
              style={{ width: 160 }}
            />
            <Select
              allowClear
              disabled={filters.assetType === 'all'}
              placeholder={t('dataGovernance.filter.status')}
              value={filters.status}
              options={statusOptions.map((v) => ({
                value: v,
                // 'all' 时下拉禁用且 options 为空，这里取 'document' 只是类型兜底。
                label: t(
                  statusLabelKey(
                    filters.assetType === 'data_source' ? 'data_source' : 'document',
                    v,
                  ),
                ),
              }))}
              onChange={(status) => setFilters((f) => ({ ...f, status }))}
              style={{ width: 130 }}
            />
            <Input
              allowClear
              placeholder={t('dataGovernance.filter.keyword')}
              value={filters.keyword}
              onChange={(e) => setFilters((f) => ({ ...f, keyword: e.target.value }))}
              onPressEnter={applyFilters}
              style={{ width: 200 }}
            />
            <Button type="primary" icon={<SearchOutlined />} onClick={applyFilters}>
              {t('dataGovernance.filter.search')}
            </Button>
            <Button onClick={resetFilters}>{t('dataGovernance.filter.reset')}</Button>
            {/* 租户管理员可维护租户默认；另有 KB 时允许进入配置维护 KB 默认。
                用全量 configKbs 判断，避免空间筛选把入口误隐藏。 */}
            {(isTenantAdmin || configKbs.length > 0) && (
              <Button icon={<SettingOutlined />} onClick={openConfig}>
                {t('dataGovernance.classification.config')}
              </Button>
            )}
            <Button
              icon={<TagsOutlined />}
              disabled={selectedRows.length === 0}
              onClick={() => setBatchOpen(true)}
            >
              {t('dataGovernance.classification.batch')}
            </Button>
            <Button icon={<SafetyOutlined />} onClick={() => setPolicyOpen(true)}>
              {t('dataGovernance.policy.title')}
            </Button>
            <Tooltip title={t('common.refresh')}>
              <Button
                aria-label={t('common.refresh')}
                icon={<ReloadOutlined />}
                onClick={() => {
                  void load();
                }}
              />
            </Tooltip>
          </div>

          {error ? (
            <Alert
              type="error"
              showIcon
              title={error}
              action={
                <Button
                  size="small"
                  onClick={() => {
                    void load();
                  }}
                >
                  {t('dataGovernance.error.retry')}
                </Button>
              }
            />
          ) : (
            <Table<DgAssetItem>
              rowKey={(row) => `${row.assetType}:${row.assetId}`}
              rowSelection={{
                selectedRowKeys: selectedRows.map((row) => `${row.assetType}:${row.assetId}`),
                onChange: (_keys, rows) => setSelectedRows(rows),
              }}
              columns={columns}
              dataSource={items}
              loading={loading}
              size="middle"
              scroll={{ x: tableScrollX }}
              locale={{
                // loading 时不要同时露出空态，否则首屏会出现「加载中 + 暂无资产」。
                emptyText: loading ? null : hasActiveFilters ? (
                  <Empty description={t('dataGovernance.empty.noData')}>
                    <Button onClick={resetFilters}>{t('dataGovernance.filter.reset')}</Button>
                  </Empty>
                ) : (
                  <Empty description={t('dataGovernance.empty.noData')} />
                ),
              }}
              pagination={{
                current: page,
                pageSize,
                total,
                showSizeChanger: true,
                pageSizeOptions: DG_PAGE_SIZE_OPTIONS,
                onChange: (nextPage, nextSize) => {
                  setPage(nextPage);
                  setPageSize(nextSize);
                },
                showTotal: (n) => t('dataGovernance.total', { total: n }),
              }}
            />
          )}
                </div>
              ),
            },
            {
              key: 'standards',
              label: t('dataGovernance.standards.title'),
              children: <StandardCatalogTab />,
            },
          ]}
        />
      </PageContent>

      <AssetDrawer
        open={Boolean(drawer)}
        assetType={drawer?.type ?? 'document'}
        assetId={drawer?.id ?? ''}
        classificationConfig={classificationConfig}
        onClose={() => setDrawer(null)}
        onSaved={() => {
          void load();
        }}
      />

      <ClassificationConfigModal
        open={configOpen}
        config={classificationConfig}
        loading={classificationConfigLoading}
        error={classificationConfigError}
        canManageTenant={isTenantAdmin}
        kbs={configKbs}
        onClose={() => setConfigOpen(false)}
        onChanged={() => {
          void loadClassificationConfig();
          void load();
        }}
        onRetry={() => {
          void loadClassificationConfig();
          void refreshConfigKbs();
        }}
      />

      <ClassificationBatchModal
        open={batchOpen}
        assets={selectedRows.map((row) => ({
          assetType: row.assetType,
          assetId: row.assetId,
        }))}
        config={classificationConfig}
        onClose={() => setBatchOpen(false)}
        onSaved={() => {
          setSelectedRows([]);
          void load();
        }}
      />

      {/* 策略不改变列表字段，保存后无需刷新列表。 */}
      <RetentionPolicyModal open={policyOpen} onClose={() => setPolicyOpen(false)} />
    </>
  );
}
