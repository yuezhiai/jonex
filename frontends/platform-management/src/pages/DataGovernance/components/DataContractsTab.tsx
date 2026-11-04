import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '@jonex/shared-lib';
import { getShellContext } from '@jonex/shell-sdk';
import {
  createDataContractDraft,
  importDataContract,
  listContractDataSources,
  listDataContracts,
  previewDataContractScaffold,
} from '@/api/dataContracts';
import { listKbOptions } from '@/api/dataOperations';
import type { DoKbOption } from '@/pages/DataOperations/types';
import type {
  DgContractDataSourceOption,
  DgContractFormat,
  DgContractScaffoldResult,
  DgDataContractItem,
  DgDataContractStatus,
} from '../types';
import {
  CONTRACT_STATUSES,
  isConflictError,
  statusColor,
  statusLabelKey,
  toDataContractItem,
} from '../contractUtils';
import { DG_PAGE_SIZE, DG_PAGE_SIZE_OPTIONS } from '../constant';
import { loadErrorText, pageAfterSizeChange } from '../standardUtils';
import { actionErrorMessage, errorMeta } from '../utils';
import DataContractDrawer from './DataContractDrawer';
import type { DataContractDrawerMode } from './DataContractDrawer';

/**
 * 数据合同列表（标准目录第四个子页签）。
 *
 * - 列表带状态/关键词筛选与分页；仅租户管理员带 `includeDrafts` 看草稿；
 * - 新建：选 KB → 选数据源 → 生成脚手架预览（只读）→ 创建草稿；
 * - 导入：把 JSON/YAML 文件原始内容交给后端解析与校验；
 * - 所有请求带序号，切筛选/翻页/关弹窗后旧响应一律丢弃。
 */
export default function DataContractsTab(): JSX.Element {
  const { t } = useTranslation();
  const tRef = useRef(t);
  useEffect(() => {
    tRef.current = t;
  }, [t]);

  const isTenantAdmin = getShellContext()?.getCurrentUser?.()?.isTenantAdmin === true;

  const [status, setStatus] = useState<DgDataContractStatus | ''>('');
  const [keyword, setKeyword] = useState('');
  const [appliedKeyword, setAppliedKeyword] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DG_PAGE_SIZE);
  const [rows, setRows] = useState<DgDataContractItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<DataContractDrawerMode | null>(null);
  const seq = useRef(0);

  // 新建 / 导入弹窗
  const [createOpen, setCreateOpen] = useState(false);
  const [kbOptions, setKbOptions] = useState<DoKbOption[]>([]);
  const [kbLoading, setKbLoading] = useState(false);
  const [kbError, setKbError] = useState<string | null>(null);
  const [selectedKb, setSelectedKb] = useState<string | undefined>();
  const [sourceOptions, setSourceOptions] = useState<DgContractDataSourceOption[]>([]);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [selectedDataSource, setSelectedDataSource] = useState<string | undefined>();
  const [createName, setCreateName] = useState('');
  const [scaffold, setScaffold] = useState<DgContractScaffoldResult | null>(null);
  const [scaffoldLoading, setScaffoldLoading] = useState(false);
  const [scaffoldError, setScaffoldError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);

  const kbSeq = useRef(0);
  const sourceSeq = useRef(0);
  const scaffoldSeq = useRef(0);
  /** 新建/导入会话代际：关弹窗或重开后，旧响应不得继续改状态。 */
  const createSeq = useRef(0);
  const importSeq = useRef(0);

  const load = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await listDataContracts({
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
        loadErrorText(err, 'dataGovernance.dataContracts.loadFailed', tRef.current),
      );
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [status, appliedKeyword, page, pageSize, isTenantAdmin]);

  useEffect(() => {
    void load();
  }, [load]);

  // 列表刷新后同步抽屉里的列表行：草稿标记/状态以最新行数据为准。
  useEffect(() => {
    setDrawer((prev) => {
      if (prev?.type !== 'view') return prev;
      const next = rows.find((row) => row.contractId === prev.row.contractId);
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

  const changeStatus = (value: DgDataContractStatus | '') => {
    setStatus(value);
    setPage(1);
  };

  // ── 新建 / 导入 ────────────────────────────────────────────────────

  const resetCreateForm = () => {
    kbSeq.current += 1;
    sourceSeq.current += 1;
    scaffoldSeq.current += 1;
    createSeq.current += 1;
    importSeq.current += 1;
    setKbLoading(false);
    setSourceLoading(false);
    setScaffoldLoading(false);
    setCreating(false);
    setImporting(false);
    setKbError(null);
    setSourceError(null);
    setScaffoldError(null);
    setSelectedKb(undefined);
    setSelectedDataSource(undefined);
    setSourceOptions([]);
    setScaffold(null);
    setCreateName('');
  };

  const loadKbOptions = useCallback(async () => {
    const my = ++kbSeq.current;
    setKbLoading(true);
    setKbError(null);
    try {
      const options = await listKbOptions();
      if (my !== kbSeq.current) return;
      setKbOptions(options);
    } catch (err: unknown) {
      if (my !== kbSeq.current) return;
      setKbOptions([]);
      setKbError(
        loadErrorText(err, 'dataGovernance.dataContracts.kbLoadFailed', tRef.current),
      );
    } finally {
      if (my === kbSeq.current) setKbLoading(false);
    }
  }, []);

  const loadSources = useCallback(async (kbId: string) => {
    const my = ++sourceSeq.current;
    setSourceLoading(true);
    setSourceError(null);
    try {
      const options = await listContractDataSources(kbId);
      if (my !== sourceSeq.current) return;
      setSourceOptions(options);
    } catch (err: unknown) {
      if (my !== sourceSeq.current) return;
      setSourceOptions([]);
      setSourceError(
        loadErrorText(
          err,
          'dataGovernance.dataContracts.dataSourceLoadFailed',
          tRef.current,
        ),
      );
    } finally {
      if (my === sourceSeq.current) setSourceLoading(false);
    }
  }, []);

  const openCreate = () => {
    resetCreateForm();
    setCreateOpen(true);
    void loadKbOptions();
  };

  const closeCreate = () => {
    setCreateOpen(false);
    resetCreateForm();
  };

  /**
   * 切换 KB / 数据源时作废在途的创建与导入请求：旧源上的响应不得改新会话状态。
   * 同时复位 loading 标记，否则被作废请求的 finally 不再复位，按钮会永久禁用。
   */
  const abandonInFlightWrites = () => {
    createSeq.current += 1;
    importSeq.current += 1;
    setCreating(false);
    setImporting(false);
  };

  const changeKb = (value: string | undefined) => {
    abandonInFlightWrites();
    // 清空 KB 时不会有新的 loadSources；必须在这里作废旧源列表请求。
    sourceSeq.current += 1;
    setSourceLoading(false);
    setSelectedKb(value);
    setSelectedDataSource(undefined);
    setSourceOptions([]);
    setSourceError(null);
    setScaffold(null);
    setScaffoldError(null);
    scaffoldSeq.current += 1;
    setScaffoldLoading(false);
    if (value) void loadSources(value);
  };

  const changeDataSource = (value: string | undefined) => {
    abandonInFlightWrites();
    setSelectedDataSource(value);
    setScaffold(null);
    setScaffoldError(null);
    scaffoldSeq.current += 1;
    setScaffoldLoading(false);
  };

  const handlePreview = async () => {
    if (!selectedDataSource) return;
    const my = ++scaffoldSeq.current;
    setScaffoldLoading(true);
    setScaffoldError(null);
    setScaffold(null);
    try {
      const result = await previewDataContractScaffold(selectedDataSource);
      if (my !== scaffoldSeq.current) return;
      setScaffold(result);
    } catch (err: unknown) {
      if (my !== scaffoldSeq.current) return;
      setScaffoldError(
        loadErrorText(
          err,
          'dataGovernance.dataContracts.scaffoldFailed',
          tRef.current,
        ),
      );
    } finally {
      if (my === scaffoldSeq.current) setScaffoldLoading(false);
    }
  };

  const showWriteError = (err: unknown) => {
    if (isConflictError(err)) {
      // 1003 有两种语义：已有草稿（uq_dc_draft）与同版本重复（uq_dc_version），
      // 不能一律按「草稿」提示。
      const text = errorMeta(err).message ?? '';
      if (text.includes('草稿')) {
        message.error(tRef.current('dataGovernance.dataContracts.draftExists'));
      } else if (text.includes('已存在')) {
        message.error(tRef.current('dataGovernance.dataContracts.versionExists'));
      } else {
        message.error(tRef.current('dataGovernance.dataContracts.conflict'));
      }
      return;
    }
    const text = actionErrorMessage(err, {
      forbiddenKey: 'dataGovernance.standards.errorForbidden',
      fallbackKey: 'dataGovernance.dataContracts.actionFailed',
    });
    message.error('key' in text ? tRef.current(text.key) : text.message);
  };

  const handleCreate = async () => {
    if (!selectedDataSource || creating || importing) return;
    const my = ++createSeq.current;
    setCreating(true);
    try {
      const name = createName.trim();
      const detail = await createDataContractDraft({
        dataSourceId: selectedDataSource,
        ...(name ? { name } : {}),
      });
      // 弹窗已关闭/重开：旧响应不关闭新弹窗、不改新会话状态。
      if (my !== createSeq.current) return;
      message.success(tRef.current('common.operationSuccessful'));
      closeCreate();
      void load();
      setDrawer({ type: 'view', row: toDataContractItem(detail) });
    } catch (err: unknown) {
      if (my !== createSeq.current) return;
      showWriteError(err);
    } finally {
      if (my === createSeq.current) setCreating(false);
    }
  };

  /** 上传即导入：读原始文本交给后端解析（前端不做 YAML/JSON 语义校验）。 */
  const handleImportFile = async (file: File) => {
    if (!selectedDataSource) {
      message.warning(tRef.current('dataGovernance.dataContracts.importRequiresDataSource'));
      return;
    }
    if (creating || importing) return;
    const my = ++importSeq.current;
    setImporting(true);
    try {
      const content = await file.text();
      if (my !== importSeq.current) return;
      const extension = file.name.toLowerCase().split('.').pop() ?? '';
      const format: DgContractFormat | '' =
        extension === 'yaml' || extension === 'yml'
          ? 'yaml'
          : extension === 'json'
            ? 'json'
            : '';
      const detail = await importDataContract({
        dataSourceId: selectedDataSource,
        content,
        format,
      });
      if (my !== importSeq.current) return;
      message.success(tRef.current('common.operationSuccessful'));
      closeCreate();
      void load();
      setDrawer({ type: 'view', row: toDataContractItem(detail) });
    } catch (err: unknown) {
      if (my !== importSeq.current) return;
      showWriteError(err);
    } finally {
      if (my === importSeq.current) setImporting(false);
    }
  };

  const columns: ColumnsType<DgDataContractItem> = [
    {
      title: t('dataGovernance.dataContracts.dataSource'),
      key: 'dataSource',
      width: 200,
      ellipsis: true,
      render: (_: unknown, row) => row.dataSourceName || row.dataSourceId || '—',
    },
    {
      title: t('dataGovernance.dataContracts.contract'),
      dataIndex: 'name',
      width: 240,
      ellipsis: true,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      width: 120,
      render: (value: DgDataContractStatus) => (
        <Tag color={statusColor(value)}>{t(statusLabelKey(value))}</Tag>
      ),
    },
    {
      title: t('dataGovernance.dataContracts.version'),
      key: 'version',
      width: 100,
      render: (_: unknown, row) => `v${row.version}`,
    },
    {
      title: t('dataGovernance.dataContracts.draft'),
      dataIndex: 'hasDraft',
      width: 140,
      render: (value: boolean, row) =>
        // 草稿行本身不再重复标「有草稿」。
        value && row.status !== 'draft' ? (
          <Tag color="gold">{t('dataGovernance.dataContracts.draft')}</Tag>
        ) : (
          '—'
        ),
    },
    {
      title: t('dataGovernance.dataContracts.updatedAt'),
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
          {t('dataGovernance.dataContracts.view')}
        </a>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space size={8} wrap>
        <Select
          value={status}
          aria-label={t('dataGovernance.dataContracts.statusFilter')}
          style={{ width: 160 }}
          onChange={(value: DgDataContractStatus | '') => changeStatus(value)}
          options={[
            { value: '', label: t('dataGovernance.dataContracts.allStatus') },
            // 草稿只有管理员能看到（includeDrafts），非管理员不提供该筛选项。
            ...CONTRACT_STATUSES.filter(
              (value) => isTenantAdmin || value !== 'draft',
            ).map((value) => ({
              value,
              label: t(statusLabelKey(value)),
            })),
          ]}
        />
        <Input
          allowClear
          value={keyword}
          placeholder={t('dataGovernance.dataContracts.keyword')}
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
          <Button icon={<PlusOutlined />} onClick={openCreate}>
            {t('dataGovernance.dataContracts.create')}
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
        <Table<DgDataContractItem>
          rowKey="contractId"
          size="middle"
          loading={loading}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1070 }}
          onRow={(record) => ({
            onClick: () => setDrawer({ type: 'view', row: record }),
            style: { cursor: 'pointer' },
          })}
          locale={{
            emptyText: <Empty description={t('dataGovernance.dataContracts.empty')} />,
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

      <Modal
        open={createOpen}
        title={t('dataGovernance.dataContracts.createTitle')}
        footer={null}
        width={680}
        destroyOnHidden
        onCancel={closeCreate}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Form layout="vertical" size="small">
            <Form.Item label={t('dataGovernance.dataContracts.kb')} required>
              <Select
                showSearch
                optionFilterProp="label"
                loading={kbLoading}
                value={selectedKb}
                onChange={(value: string | undefined) => changeKb(value)}
                placeholder={t('dataGovernance.dataContracts.selectKb')}
                options={kbOptions.map((option) => ({
                  value: option.id,
                  label: option.name,
                }))}
              />
            </Form.Item>
            <Form.Item label={t('dataGovernance.dataContracts.dataSource')} required>
              <Select
                showSearch
                optionFilterProp="label"
                loading={sourceLoading}
                disabled={!selectedKb}
                value={selectedDataSource}
                onChange={(value: string | undefined) => changeDataSource(value)}
                placeholder={t('dataGovernance.dataContracts.selectDataSource')}
                options={sourceOptions.map((option) => ({
                  value: option.id,
                  label: option.name,
                }))}
              />
            </Form.Item>
            <Form.Item label={t('dataGovernance.dataContracts.name')}>
              <Input
                value={createName}
                onChange={(event) => setCreateName(event.target.value)}
                placeholder={t('dataGovernance.dataContracts.namePlaceholder')}
              />
            </Form.Item>
          </Form>

          {kbError && (
            <Alert
              type="error"
              showIcon
              message={kbError}
              action={
                <Button size="small" onClick={() => void loadKbOptions()}>
                  {t('common.retry')}
                </Button>
              }
            />
          )}
          {sourceError && (
            <Alert
              type="error"
              showIcon
              message={sourceError}
              action={
                <Button
                  size="small"
                  onClick={() => {
                    if (selectedKb) void loadSources(selectedKb);
                  }}
                >
                  {t('common.retry')}
                </Button>
              }
            />
          )}
          {scaffoldError && <Alert type="error" showIcon message={scaffoldError} />}

          {scaffold && (
            <>
              <Alert
                type="success"
                showIcon
                message={t('dataGovernance.dataContracts.scaffoldStats', {
                  tables: scaffold.stats.tables,
                  columns: scaffold.stats.columns,
                  skippedTables: scaffold.stats.skippedTables,
                  skippedColumns: scaffold.stats.skippedColumns,
                })}
              />
              {scaffold.warnings.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  message={t('dataGovernance.dataContracts.warnings')}
                  description={
                    <ul style={{ margin: 0, paddingLeft: 18 }}>
                      {scaffold.warnings.map((warning, index) => (
                        // 后端可能返回重复文案：用索引做 key，避免重复 key 丢行。
                        <li key={`${index}-${warning}`}>{warning}</li>
                      ))}
                    </ul>
                  }
                />
              )}
            </>
          )}

          <Space size={8} wrap>
            <Button
              loading={scaffoldLoading}
              disabled={!selectedDataSource}
              onClick={() => void handlePreview()}
            >
              {t('dataGovernance.dataContracts.scaffold')}
            </Button>
            <Button
              type="primary"
              loading={creating}
              disabled={!selectedDataSource || importing}
              onClick={() => void handleCreate()}
            >
              {t('dataGovernance.dataContracts.createDraft')}
            </Button>
            <Upload
              accept=".json,.yaml,.yml"
              showUploadList={false}
              disabled={!selectedDataSource || creating || importing}
              beforeUpload={(file) => {
                void handleImportFile(file);
                // 返回 false：不走上传管线，文件内容由 handleImportFile 读取。
                return false;
              }}
            >
              <Button
                icon={<UploadOutlined />}
                loading={importing}
                disabled={!selectedDataSource || creating}
              >
                {t('dataGovernance.dataContracts.import')}
              </Button>
            </Upload>
            <Button onClick={closeCreate}>{t('common.cancel')}</Button>
          </Space>
          <Typography.Text type="secondary">
            {t('dataGovernance.dataContracts.importHint')}
          </Typography.Text>
        </Space>
      </Modal>

      <DataContractDrawer
        open={Boolean(drawer)}
        mode={drawer}
        onClose={() => setDrawer(null)}
        onChanged={() => void load()}
        onSelectRow={(row) => setDrawer({ type: 'view', row })}
      />
    </Space>
  );
}
