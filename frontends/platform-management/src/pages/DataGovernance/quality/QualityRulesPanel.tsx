import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Empty, Input, Modal, Popconfirm, Select, Space, Table, Tag, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import { listDataContracts } from '@/api/dataContracts';
import {
  deleteQualityRule,
  importQualityRules,
  listQualityDataSourceOptions,
  listQualityRules,
} from '@/api/quality';
import type {
  DgQualityRule,
  DgQualityRuleListParams,
} from '../types';
import {
  QUALITY_CHECK_TYPES,
  QUALITY_DIMENSIONS,
  QUALITY_SEVERITIES,
  checkTypeLabelKey,
  dimensionLabelKey,
  ruleSummary,
  severityColor,
  severityLabelKey,
} from '../qualityUtils';
import QualityRuleDrawer from './QualityRuleDrawer';

interface Props {
  canWrite: boolean;
}

interface Option {
  value: string;
  label: string;
}

const PAGE_SIZE = 20;
const IMPORT_PAGE_SIZE = 100;

export default function QualityRulesPanel({ canWrite }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<DgQualityRule[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [dimension, setDimension] = useState<DgQualityRuleListParams['dimension']>('');
  const [severity, setSeverity] = useState<DgQualityRuleListParams['severity']>('');
  const [checkType, setCheckType] = useState<DgQualityRuleListParams['checkType']>('');
  const [dataSourceId, setDataSourceId] = useState('');
  const [assetType, setAssetType] = useState<DgQualityRuleListParams['assetType']>('');
  const [enabled, setEnabled] = useState<DgQualityRuleListParams['enabled']>();
  const [dataSourceOptions, setDataSourceOptions] = useState<Option[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<DgQualityRule | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importing, setImporting] = useState(false);
  const [contractsLoading, setContractsLoading] = useState(false);
  const [contractId, setContractId] = useState('');
  const [contractOptions, setContractOptions] = useState<Option[]>([]);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await listQualityRules({
        page, pageSize: PAGE_SIZE,
        ...(keyword ? { keyword } : {}),
        ...(dimension ? { dimension } : {}),
        ...(severity ? { severity } : {}),
        ...(checkType ? { checkType } : {}),
        ...(dataSourceId ? { dataSourceId } : {}),
        ...(assetType ? { assetType } : {}),
        ...(enabled === 0 || enabled === 1 ? { enabled } : {}),
      });
      if (seq !== requestSeq.current) return;
      setItems(result.items);
      setTotal(result.total);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(err instanceof Error ? err.message : t('dataGovernance.quality.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [assetType, checkType, dataSourceId, dimension, enabled, keyword, page, severity, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void listQualityDataSourceOptions().then((options) => {
      setDataSourceOptions(options.map((item) => ({ value: item.id, label: item.name })));
    });
  }, []);

  const handleDelete = async (rule: DgQualityRule) => {
    try {
      await deleteQualityRule(rule.id);
      message.success(t('dataGovernance.quality.rule.deleted'));
      void load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('dataGovernance.quality.rule.deleteFailed'));
    }
  };

  const openImport = async () => {
    setImportOpen(true);
    setContractId('');
    setContractsLoading(true);
    try {
      const result = await listDataContracts({
        status: 'published', page: 1, pageSize: IMPORT_PAGE_SIZE,
      });
      setContractOptions(result.items.map((contract) => ({
        value: contract.contractId,
        label: `${contract.name} · ${contract.dataSourceName ?? contract.dataSourceId} · v${contract.version}`,
      })));
    } catch (err) {
      setContractOptions([]);
      message.error(err instanceof Error ? err.message : t('dataGovernance.quality.rule.importFailed'));
    } finally {
      setContractsLoading(false);
    }
  };

  const handleImport = async () => {
    if (!contractId) return;
    setImporting(true);
    try {
      const result = await importQualityRules(contractId);
      message.success(t('dataGovernance.quality.rule.importDone', {
        created: result.created, skipped: result.skipped,
      }));
      if (result.warnings.length > 0) {
        message.warning(result.warnings.slice(0, 3).join('; '));
      }
      setImportOpen(false);
      void load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('dataGovernance.quality.rule.importFailed'));
    } finally {
      setImporting(false);
    }
  };

  const columns: ColumnsType<DgQualityRule> = [
    {
      title: t('dataGovernance.quality.rule.name'),
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
    },
    {
      title: t('dataGovernance.quality.rule.asset'),
      dataIndex: 'assetId',
      key: 'assetId',
      ellipsis: true,
    },
    {
      title: t('dataGovernance.quality.rule.rule'),
      key: 'rule',
      render: (_, row) => ruleSummary(row, (key, params) => t(key, params)),
    },
    {
      title: t('dataGovernance.quality.rule.dimension'),
      dataIndex: 'dimension',
      key: 'dimension',
      render: (value: string) => <Tag>{t(dimensionLabelKey(value))}</Tag>,
    },
    {
      title: t('dataGovernance.quality.rule.severity'),
      dataIndex: 'severity',
      key: 'severity',
      render: (value: string) => (
        <Tag color={severityColor(value)}>{t(severityLabelKey(value))}</Tag>
      ),
    },
    {
      title: t('dataGovernance.quality.rule.checkType'),
      dataIndex: 'checkType',
      key: 'checkType',
      render: (value: string) => t(checkTypeLabelKey(value)),
    },
    {
      title: t('dataGovernance.quality.rule.enabled'),
      dataIndex: 'enabled',
      key: 'enabled',
      render: (value: number) => (
        <Tag color={value === 1 ? 'green' : 'default'}>
          {value === 1
            ? t('dataGovernance.quality.rule.on')
            : t('dataGovernance.quality.rule.off')}
        </Tag>
      ),
    },
    {
      title: t('dataGovernance.quality.rule.actions'),
      key: 'actions',
      render: (_, row) => (
        <Space>
          <Button
            type="link"
            size="small"
            disabled={!canWrite}
            onClick={() => { setEditing(row); setDrawerOpen(true); }}
          >
            {t('common.edit')}
          </Button>
          <Popconfirm
            title={t('dataGovernance.quality.rule.confirmDelete')}
            disabled={!canWrite}
            onConfirm={() => void handleDelete(row)}
          >
            <Button type="link" size="small" danger disabled={!canWrite}>
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="yx-dg-page">
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder={t('dataGovernance.quality.rule.filterDataSource')}
          value={dataSourceId || undefined}
          style={{ width: 190 }}
          options={dataSourceOptions}
          onChange={(value) => { setDataSourceId(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.rule.assetType')}
          value={assetType || undefined}
          style={{ width: 130 }}
          options={[
            { value: 'table', label: t('dataGovernance.quality.rule.table') },
            { value: 'column', label: t('dataGovernance.quality.rule.column') },
          ]}
          onChange={(value) => { setAssetType(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.rule.filterEnabled')}
          value={enabled}
          style={{ width: 130 }}
          options={[
            { value: 1, label: t('dataGovernance.quality.rule.on') },
            { value: 0, label: t('dataGovernance.quality.rule.off') },
          ]}
          onChange={(value) => { setEnabled(value); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.rule.dimension')}
          value={dimension || undefined}
          style={{ width: 150 }}
          options={QUALITY_DIMENSIONS.map((value) => ({
            value, label: t(dimensionLabelKey(value)),
          }))}
          onChange={(value) => { setDimension(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.rule.severity')}
          value={severity || undefined}
          style={{ width: 130 }}
          options={QUALITY_SEVERITIES.map((value) => ({
            value, label: t(severityLabelKey(value)),
          }))}
          onChange={(value) => { setSeverity(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.rule.checkType')}
          value={checkType || undefined}
          style={{ width: 140 }}
          options={QUALITY_CHECK_TYPES.map((value) => ({
            value, label: t(checkTypeLabelKey(value)),
          }))}
          onChange={(value) => { setCheckType(value ?? ''); setPage(1); }}
        />
        <Input.Search
          allowClear
          placeholder={t('dataGovernance.quality.rule.keyword')}
          style={{ width: 220 }}
          onSearch={(value) => { setKeyword(value.trim()); setPage(1); }}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          {t('common.refresh')}
        </Button>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          disabled={!canWrite}
          onClick={() => { setEditing(null); setDrawerOpen(true); }}
        >
          {t('dataGovernance.quality.rule.create')}
        </Button>
        <Button disabled={!canWrite} onClick={() => void openImport()}>
          {t('dataGovernance.quality.rule.import')}
        </Button>
      </Space>

      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>}
        />
      ) : (
        <Table<DgQualityRule>
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={loading}
          size="middle"
          scroll={{ x: 1100 }}
          locale={{
            emptyText: loading ? null : <Empty description={t('dataGovernance.quality.empty')} />,
          }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            onChange: setPage,
            showTotal: (n) => t('dataGovernance.total', { total: n }),
          }}
        />
      )}

      <QualityRuleDrawer
        open={drawerOpen}
        rule={editing}
        canWrite={canWrite}
        onClose={() => setDrawerOpen(false)}
        onSaved={() => void load()}
      />

      <Modal
        open={importOpen}
        title={t('dataGovernance.quality.rule.importTitle')}
        confirmLoading={importing}
        okButtonProps={{ disabled: !contractId }}
        onCancel={() => setImportOpen(false)}
        onOk={() => void handleImport()}
        okText={t('dataGovernance.quality.rule.import')}
        width={620}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Select
            showSearch
            allowClear
            optionFilterProp="label"
            value={contractId || undefined}
            style={{ width: '100%' }}
            loading={contractsLoading}
            placeholder={t('dataGovernance.quality.rule.importSelect')}
            options={contractOptions}
            onChange={(value) => setContractId(value ?? '')}
          />
          {!contractsLoading && contractOptions.length === 0 ? (
            <Empty description={t('dataGovernance.quality.rule.importEmpty')} />
          ) : null}
        </Space>
      </Modal>
    </div>
  );
}
