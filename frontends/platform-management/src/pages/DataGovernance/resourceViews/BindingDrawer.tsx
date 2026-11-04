/**
 * W2.6 落标绑定：新建抽屉 + 绑定列表抽屉。
 *
 * 资产选择复用 W2.5 质量规则的 KB → 数据源 → 表/列 级联（`AssetPicker`），
 * 标准资产只列 published。写操作仅租户管理员可见；乐观锁冲突（1003）提示刷新。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Drawer, Empty, Input, Popconfirm, Select, Space, Table, Tag, message,
} from 'antd';

import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '@jonex/shared-lib';

import { listContractDataSources } from '@/api/dataContracts';
import { getAsset, listAssets } from '@/api/dataGovernance';
import { listKbOptions } from '@/api/dataOperations';
import {
  createStandardBinding,
  deleteStandardBinding,
  listStandardBindings,
  updateStandardBinding,
} from '@/api/resourceViews';
import { listStandardAssets } from '@/api/standardAssets';
import type { DgStandardAssetItem, DgStandardBinding } from '../types';
import { errorMeta } from '../utils';
import { kindLabelKey } from '../standardAssetUtils';
import { parseQualityAssetId } from '../qualityUtils';

interface Option {
  value: string;
  label: string;
}

const TABLE_PAGE_SIZE = 100;
const STANDARD_PAGE_SIZE = 100;
const BINDING_PAGE_SIZE = 10;

function isConflict(err: unknown): boolean {
  return errorMeta(err).bizCode === 1003;
}

async function loadAllTables(kbId: string) {
  const items = [];
  for (let page = 1; ; page += 1) {
    const result = await listAssets({
      assetType: 'table', kbId, page, pageSize: TABLE_PAGE_SIZE,
    });
    items.push(...result.items);
    if (result.items.length < TABLE_PAGE_SIZE || items.length >= result.total) break;
  }
  return items;
}

async function loadPublishedStandards(): Promise<DgStandardAssetItem[]> {
  const items: DgStandardAssetItem[] = [];
  for (let page = 1; ; page += 1) {
    const result = await listStandardAssets({
      status: 'published', page, pageSize: STANDARD_PAGE_SIZE,
    });
    items.push(...result.items);
    if (result.items.length < STANDARD_PAGE_SIZE || items.length >= result.total) break;
  }
  return items;
}

// ── 资产选择器（KB → 数据源 → 表 → 列） ─────────────────────────────

export interface AssetSelection {
  kbId: string;
  dataSourceId: string;
  tableAssetId: string;
  tableName: string;
  columnName: string;
  /** 空 = 尚未选到表级资产。 */
  assetType: 'table' | 'column' | '';
  assetId: string;
}

export const EMPTY_ASSET_SELECTION: AssetSelection = {
  kbId: '',
  dataSourceId: '',
  tableAssetId: '',
  tableName: '',
  columnName: '',
  assetType: '',
  assetId: '',
};

interface AssetPickerProps {
  value: AssetSelection;
  onChange: (next: AssetSelection) => void;
  disabled?: boolean;
}

export function AssetPicker({ value, onChange, disabled }: AssetPickerProps) {
  const { t } = useTranslation();
  const [kbOptions, setKbOptions] = useState<Option[]>([]);
  const [dataSourceOptions, setDataSourceOptions] = useState<Option[]>([]);
  const [tableOptions, setTableOptions] = useState<Option[]>([]);
  const [columnOptions, setColumnOptions] = useState<Option[]>([]);
  const [loadingTables, setLoadingTables] = useState(false);
  const tableSeq = useRef(0);
  const columnSeq = useRef(0);

  useEffect(() => {
    let cancelled = false;
    void listKbOptions()
      .then((options) => {
        if (cancelled) return;
        setKbOptions(options.map((item) => ({ value: item.id, label: item.name })));
      })
      .catch(() => {
        if (!cancelled) setKbOptions([]);
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!value.kbId) {
      setDataSourceOptions([]);
      return;
    }
    let cancelled = false;
    void listContractDataSources(value.kbId)
      .then((options) => {
        if (cancelled) return;
        setDataSourceOptions(options.map((item) => ({ value: item.id, label: item.name })));
      })
      .catch(() => {
        if (!cancelled) setDataSourceOptions([]);
      });
    return () => { cancelled = true; };
  }, [value.kbId]);

  useEffect(() => {
    if (!value.kbId || !value.dataSourceId) {
      setTableOptions([]);
      setLoadingTables(false);
      return;
    }
    const seq = ++tableSeq.current;
    setLoadingTables(true);
    void loadAllTables(value.kbId)
      .then((items) => {
        if (seq !== tableSeq.current) return;
        setTableOptions(items
          .filter((item) => item.sourceId === value.dataSourceId)
          .map((item) => ({ value: item.assetId, label: item.name || item.assetId })));
      })
      .catch(() => {
        if (seq !== tableSeq.current) return;
        setTableOptions([]);
      })
      .finally(() => {
        if (seq === tableSeq.current) setLoadingTables(false);
      });
    return () => { tableSeq.current += 1; };
  }, [value.kbId, value.dataSourceId]);

  useEffect(() => {
    if (!value.tableAssetId) {
      setColumnOptions([]);
      return;
    }
    const seq = ++columnSeq.current;
    void getAsset('table', value.tableAssetId)
      .then((detail) => {
        if (seq !== columnSeq.current) return;
        setColumnOptions((detail.columns ?? []).map((column) => ({
          value: column.name,
          label: column.displayName ? `${column.displayName} (${column.name})` : column.name,
        })));
      })
      .catch(() => {
        if (seq !== columnSeq.current) return;
        setColumnOptions([]);
      });
    return () => { columnSeq.current += 1; };
  }, [value.tableAssetId]);

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Select
        showSearch
        allowClear
        optionFilterProp="label"
        value={value.kbId || undefined}
        style={{ width: '100%' }}
        disabled={disabled}
        placeholder={t('dataGovernance.resourceViews.binding.selectKb')}
        options={kbOptions}
        onChange={(next) => onChange({ ...EMPTY_ASSET_SELECTION, kbId: next ?? '' })}
      />
      <Space.Compact style={{ width: '100%' }}>
        <Select
          showSearch
          allowClear
          optionFilterProp="label"
          value={value.dataSourceId || undefined}
          style={{ width: 220 }}
          disabled={disabled || !value.kbId}
          placeholder={t('dataGovernance.resourceViews.binding.selectDataSource')}
          options={dataSourceOptions}
          onChange={(next) => onChange({
            ...EMPTY_ASSET_SELECTION,
            kbId: value.kbId,
            dataSourceId: next ?? '',
          })}
        />
        <Select
          showSearch
          allowClear
          optionFilterProp="label"
          value={value.tableAssetId || undefined}
          style={{ width: 240 }}
          disabled={disabled || !value.dataSourceId}
          loading={loadingTables}
          placeholder={t('dataGovernance.resourceViews.binding.selectTable')}
          options={tableOptions}
          onChange={(next) => {
            const tableAssetId = next ?? '';
            const tableName = tableAssetId
              ? parseQualityAssetId(tableAssetId, 'table').table
              : '';
            onChange({
              ...EMPTY_ASSET_SELECTION,
              kbId: value.kbId,
              dataSourceId: value.dataSourceId,
              tableAssetId,
              tableName,
              assetType: tableAssetId ? 'table' : '',
              assetId: tableAssetId,
            });
          }}
        />
      </Space.Compact>
      <Select
        showSearch
        allowClear
        optionFilterProp="label"
        value={value.columnName || undefined}
        style={{ width: '100%' }}
        disabled={disabled || !value.tableAssetId}
        placeholder={t('dataGovernance.resourceViews.binding.selectColumn')}
        options={columnOptions}
        onChange={(next) => {
          const columnName = next ?? '';
          const assetId = columnName
            ? `${value.tableAssetId}.${columnName}`
            : value.tableAssetId;
          onChange({
            ...value,
            columnName,
            assetType: value.tableAssetId ? (columnName ? 'column' : 'table') : '',
            assetId: value.tableAssetId ? assetId : '',
          });
        }}
      />
      {value.assetId ? (
        <Tag color={value.assetType === 'column' ? 'blue' : 'default'}>
          {value.assetType === 'column'
            ? t('dataGovernance.resourceViews.assetType.column')
            : t('dataGovernance.resourceViews.assetType.table')}
          {' · '}
          {value.assetId}
        </Tag>
      ) : null}
    </Space>
  );
}

// ── 新建绑定 ─────────────────────────────────────────────────────────

interface BindingDrawerProps {
  open: boolean;
  canWrite: boolean;
  onClose: () => void;
  onSaved: () => void;
}

export function BindingDrawer({
  open, canWrite, onClose, onSaved,
}: BindingDrawerProps) {
  const { t } = useTranslation();
  const [standards, setStandards] = useState<DgStandardAssetItem[]>([]);
  const [standardsError, setStandardsError] = useState<string | null>(null);
  const [standardKey, setStandardKey] = useState('');
  const [selection, setSelection] = useState<AssetSelection>(EMPTY_ASSET_SELECTION);
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const loadSeq = useRef(0);

  useEffect(() => {
    if (!open) return;
    const seq = ++loadSeq.current;
    setStandardKey('');
    setStandardsError(null);
    setSelection(EMPTY_ASSET_SELECTION);
    setNote('');
    void loadPublishedStandards()
      .then((items) => {
        if (seq !== loadSeq.current) return;
        setStandards(items);
      })
      .catch(() => {
        if (seq !== loadSeq.current) return;
        setStandards([]);
        setStandardsError(t('dataGovernance.resourceViews.standardsLoadFailed'));
      });
    return () => { loadSeq.current += 1; };
  }, [open]);

  const standardOptions = useMemo(
    () => standards.map((item) => ({
      value: `${item.kind}::${item.code}`,
      label: `${t(kindLabelKey(item.kind))} · ${item.code} · ${item.name}`,
    })),
    [standards, t],
  );

  const save = async () => {
    if (!canWrite || saving) return;
    const standard = standards.find(
      (item) => `${item.kind}::${item.code}` === standardKey,
    );
    if (!standard) {
      message.error(t('dataGovernance.resourceViews.binding.standardRequired'));
      return;
    }
    if (!selection.assetId || !selection.assetType) {
      message.error(t('dataGovernance.resourceViews.binding.assetRequired'));
      return;
    }
    setSaving(true);
    try {
      await createStandardBinding({
        standardKind: standard.kind,
        standardCode: standard.code,
        assetType: selection.assetType,
        assetId: selection.assetId,
        source: 'manual',
        ...(note.trim() ? { note: note.trim() } : {}),
      });
      message.success(t('dataGovernance.resourceViews.binding.saved'));
      onSaved();
      onClose();
    } catch (err) {
      message.error(
        isConflict(err)
          ? t('dataGovernance.resourceViews.binding.conflict')
          : (err instanceof Error && err.message
            ? err.message
            : t('dataGovernance.resourceViews.binding.saveFailed')),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      open={open}
      width={640}
      title={t('dataGovernance.resourceViews.binding.createTitle')}
      onClose={onClose}
      destroyOnClose
      footer={(
        <Space style={{ float: 'right' }}>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button
            type="primary"
            loading={saving}
            disabled={!canWrite}
            onClick={() => void save()}
          >
            {t('common.save')}
          </Button>
        </Space>
      )}
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {standardsError ? <Alert type="error" showIcon message={standardsError} /> : null}
        <Select
          showSearch
          allowClear
          optionFilterProp="label"
          value={standardKey || undefined}
          style={{ width: '100%' }}
          placeholder={t('dataGovernance.resourceViews.binding.selectStandard')}
          options={standardOptions}
          onChange={(next) => setStandardKey(next ?? '')}
        />
        <AssetPicker value={selection} onChange={setSelection} disabled={!canWrite} />
        <Input
          value={note}
          placeholder={t('dataGovernance.resourceViews.binding.notePlaceholder')}
          onChange={(event) => setNote(event.target.value)}
        />
      </Space>
    </Drawer>
  );
}

// ── 绑定列表（查看 / 停用 / 启用 / 删除） ────────────────────────────

export interface BindingTarget {
  kind: DgStandardBinding['standardKind'];
  code: string;
  name: string;
}

interface BindingListDrawerProps {
  open: boolean;
  standard: BindingTarget | null;
  canWrite: boolean;
  onClose: () => void;
  onChanged: () => void;
}

export function BindingListDrawer({
  open, standard, canWrite, onClose, onChanged,
}: BindingListDrawerProps) {
  const { t } = useTranslation();
  const [items, setItems] = useState<DgStandardBinding[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    if (!open || !standard) return;
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await listStandardBindings({
        standardKind: standard.kind,
        standardCode: standard.code,
        page,
        pageSize: BINDING_PAGE_SIZE,
      });
      if (seq !== requestSeq.current) return;
      setItems(result.items);
      setTotal(result.total);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setItems([]);
      setTotal(0);
      setError(err instanceof Error && err.message
        ? err.message
        : t('dataGovernance.resourceViews.bindings.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [open, standard, page, t]);

  useEffect(() => {
    if (!open) {
      requestSeq.current += 1;
      setItems([]);
      setTotal(0);
      setPage(1);
      setError(null);
      setLoading(false);
      return;
    }
    void load();
  }, [open, load]);

  useEffect(() => {
    if (open) setPage(1);
  }, [open, standard?.kind, standard?.code]);

  const toggleStatus = async (row: DgStandardBinding) => {
    if (!canWrite) return;
    try {
      await updateStandardBinding(row.id, {
        expectedRevision: row.revision,
        status: row.status === 'active' ? 'disabled' : 'active',
      });
      message.success(t('dataGovernance.resourceViews.bindings.updated'));
      void load();
      onChanged();
    } catch (err) {
      message.error(
        isConflict(err)
          ? t('dataGovernance.resourceViews.binding.conflict')
          : (err instanceof Error && err.message
            ? err.message
            : t('dataGovernance.resourceViews.bindings.updateFailed')),
      );
      if (isConflict(err)) {
        void load();
        onChanged();
      }
    }
  };

  const remove = async (row: DgStandardBinding) => {
    if (!canWrite) return;
    try {
      await deleteStandardBinding(row.id);
      message.success(t('dataGovernance.resourceViews.bindings.deleted'));
      void load();
      onChanged();
    } catch (err) {
      message.error(
        isConflict(err)
          ? t('dataGovernance.resourceViews.binding.conflict')
          : (err instanceof Error && err.message
            ? err.message
            : t('dataGovernance.resourceViews.bindings.deleteFailed')),
      );
      if (isConflict(err)) {
        void load();
        onChanged();
      }
    }
  };

  const columns: ColumnsType<DgStandardBinding> = [
    {
      title: t('dataGovernance.resourceViews.binding.asset'),
      key: 'asset',
      render: (_value, row) => (
        <Space direction="vertical" size={0}>
          <Tag>{t(`dataGovernance.resourceViews.assetType.${row.assetType}`)}</Tag>
          <span>{row.assetId}</span>
        </Space>
      ),
    },
    {
      title: t('dataGovernance.resourceViews.bindings.source'),
      dataIndex: 'source',
      width: 120,
      render: (value: string) => t(`dataGovernance.resourceViews.bindingSource.${value}`),
    },
    {
      title: t('dataGovernance.resourceViews.bindings.status'),
      dataIndex: 'status',
      width: 100,
      render: (value: DgStandardBinding['status']) => (
        <Tag color={value === 'active' ? 'green' : 'default'}>
          {t(`dataGovernance.resourceViews.bindingStatus.${value}`)}
        </Tag>
      ),
    },
    {
      title: t('dataGovernance.resourceViews.bindings.note'),
      dataIndex: 'note',
      ellipsis: true,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.resourceViews.bindings.updatedAt'),
      dataIndex: 'updatedAt',
      width: 170,
      render: (value: string | null) => (value ? formatDateTime(value) : '—'),
    },
    // 非管理员连写操作入口都不渲染（后端仍会兜底校验）。
    ...(canWrite
      ? [{
        title: t('dataGovernance.resourceViews.bindings.actions'),
        key: 'actions',
        width: 150,
        render: (_value: unknown, row: DgStandardBinding) => (
          <Space>
            <Button
              type="link"
              size="small"
              onClick={() => void toggleStatus(row)}
            >
              {row.status === 'active'
                ? t('dataGovernance.resourceViews.bindings.disable')
                : t('dataGovernance.resourceViews.bindings.enable')}
            </Button>
            <Popconfirm
              title={t('dataGovernance.resourceViews.bindings.confirmDelete')}
              onConfirm={() => void remove(row)}
            >
              <Button type="link" size="small" danger>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          </Space>
        ),
      }]
      : []),
  ];

  return (
    <Drawer
      open={open}
      width={820}
      title={standard
        ? t('dataGovernance.resourceViews.bindings.title', {
          name: standard.name || standard.code,
        })
        : t('dataGovernance.resourceViews.bindings.titleGeneric')}
      onClose={onClose}
      destroyOnClose
    >
      {error ? (
        <Empty description={error}>
          <Button onClick={() => void load()}>{t('common.retry')}</Button>
        </Empty>
      ) : (
        <Table<DgStandardBinding>
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={loading}
          size="middle"
          scroll={{ x: 760 }}
          locale={{
            emptyText: loading
              ? null
              : <Empty description={t('dataGovernance.resourceViews.bindings.empty')} />,
          }}
          pagination={{
            current: page,
            pageSize: BINDING_PAGE_SIZE,
            total,
            onChange: setPage,
            showTotal: (n) => t('dataGovernance.total', { total: n }),
          }}
        />
      )}
    </Drawer>
  );
}
