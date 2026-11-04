import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Card, Empty, Modal, Select, Space, Table, Tag, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  CopyOutlined, DownloadOutlined, PlayCircleOutlined, ReloadOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import { listContractDataSources } from '@/api/dataContracts';
import { listKbOptions } from '@/api/dataOperations';
import {
  createQualityRun,
  listQualityDataSourceOptions,
  listQualityRules,
  listQualityRuns,
  previewQualitySql,
} from '@/api/quality';
import type {
  DgQualityRun,
  DgQualityRunListParams,
  DgQualitySqlItem,
} from '../types';
import { buildSqlText, runStatusColor, sqlFilename } from '../qualityUtils';
import QualityRunDrawer from './QualityRunDrawer';

interface Props {
  canWrite: boolean;
}

interface Option {
  value: string;
  label: string;
}

const PAGE_SIZE = 20;

export default function QualityRunsPanel({ canWrite }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<DgQualityRun[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<DgQualityRunListParams['status']>('');
  const [filterDataSourceId, setFilterDataSourceId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dataSourceOptions, setDataSourceOptions] = useState<Option[]>([]);

  const [createOpen, setCreateOpen] = useState(false);
  const [kbId, setKbId] = useState('');
  const [kbOptions, setKbOptions] = useState<Option[]>([]);
  const [dataSourceId, setDataSourceId] = useState('');
  const [createDataSourceOptions, setCreateDataSourceOptions] = useState<Option[]>([]);
  const [ruleIds, setRuleIds] = useState<string[]>([]);
  const [ruleOptions, setRuleOptions] = useState<Option[]>([]);
  const [dialect, setDialect] = useState('tsql');
  const [preview, setPreview] = useState<DgQualitySqlItem[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [creating, setCreating] = useState(false);
  const [lastCreated, setLastCreated] = useState<{
    runId: string;
    rules: DgQualitySqlItem[];
  } | null>(null);
  const [drawerRunId, setDrawerRunId] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await listQualityRuns({
        page,
        pageSize: PAGE_SIZE,
        ...(status ? { status } : {}),
        ...(filterDataSourceId ? { dataSourceId: filterDataSourceId } : {}),
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
  }, [filterDataSourceId, page, status, t]);

  useEffect(() => {
    void load();
  }, [load]);

  // 数据源筛选项：跨 KB 汇总数据库源；失败时保持空列表，不阻塞列表。
  useEffect(() => {
    void listQualityDataSourceOptions().then((options) => {
      setDataSourceOptions(options.map((item) => ({ value: item.id, label: item.name })));
    });
  }, []);

  const resetForm = () => {
    setKbId('');
    setKbOptions([]);
    setDataSourceId('');
    setCreateDataSourceOptions([]);
    setRuleIds([]);
    setRuleOptions([]);
    setDialect('tsql');
    setPreview([]);
    setWarnings([]);
  };

  const openCreate = async () => {
    resetForm();
    setCreateOpen(true);
    try {
      const options = await listKbOptions();
      setKbOptions(options.map((item) => ({ value: item.id, label: item.name })));
    } catch {
      setKbOptions([]);
    }
  };

  const handleKbChange = async (value: string | undefined) => {
    const nextKb = value ?? '';
    setKbId(nextKb);
    setDataSourceId('');
    setCreateDataSourceOptions([]);
    setRuleIds([]);
    setRuleOptions([]);
    setPreview([]);
    setWarnings([]);
    if (!nextKb) return;
    try {
      const options = await listContractDataSources(nextKb);
      setCreateDataSourceOptions(options.map((item) => ({ value: item.id, label: item.name })));
    } catch {
      setCreateDataSourceOptions([]);
    }
  };

  const handleDataSourceChange = async (value: string | undefined) => {
    const nextDataSource = value ?? '';
    setDataSourceId(nextDataSource);
    setRuleIds([]);
    setPreview([]);
    setWarnings([]);
    if (!nextDataSource) {
      setRuleOptions([]);
      return;
    }
    try {
      const result = await listQualityRules({
        dataSourceId: nextDataSource, page: 1, pageSize: 100,
      });
      setRuleOptions(result.items.map((rule) => ({
        value: rule.id,
        label: `${rule.name} · ${rule.assetId}`,
      })));
    } catch {
      setRuleOptions([]);
    }
  };

  const handlePreview = async () => {
    if (!dataSourceId) {
      message.warning(t('dataGovernance.quality.run.dataSourceRequired'));
      return;
    }
    try {
      const result = await previewQualitySql({
        dataSourceId,
        ...(ruleIds.length > 0 ? { ruleIds } : {}),
        dialect,
      });
      setPreview(result.rules);
      setWarnings(result.warnings);
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('dataGovernance.quality.run.previewFailed'));
    }
  };

  const handleCreate = async () => {
    if (!dataSourceId) {
      message.warning(t('dataGovernance.quality.run.dataSourceRequired'));
      return;
    }
    setCreating(true);
    try {
      const result = await createQualityRun({
        dataSourceId,
        ...(ruleIds.length > 0 ? { ruleIds } : {}),
        dialect,
      });
      // 保留本次创建返回的 SQL：关闭弹窗后页面仍需可复制/下载。
      setPreview(result.rules);
      setWarnings(result.warnings);
      setLastCreated({ runId: result.run.id, rules: result.rules });
      setCreateOpen(false);
      message.success(t('dataGovernance.quality.run.created'));
      void load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('dataGovernance.quality.run.createFailed'));
    } finally {
      setCreating(false);
    }
  };

  const createdSqlText = () => buildSqlText(
    lastCreated?.rules ?? [],
    t('dataGovernance.quality.run.externalSql'),
  );

  const handleCopyCreated = async () => {
    try {
      await navigator.clipboard.writeText(createdSqlText());
      message.success(t('dataGovernance.quality.run.copied'));
    } catch {
      message.error(t('dataGovernance.quality.run.copyFailed'));
    }
  };

  const handleDownloadCreated = () => {
    const blob = new Blob([createdSqlText()], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = sqlFilename(lastCreated?.runId);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  };

  const columns: ColumnsType<DgQualityRun> = [
    { title: 'ID', dataIndex: 'id', key: 'id', ellipsis: true },
    { title: t('dataGovernance.quality.run.dataSource'), dataIndex: 'dataSourceId', key: 'dataSourceId' },
    {
      title: t('dataGovernance.quality.run.status'),
      dataIndex: 'status',
      key: 'status',
      render: (value: string) => (
        <Tag color={runStatusColor(value)}>{t(`dataGovernance.quality.runStatus.${value}`)}</Tag>
      ),
    },
    {
      title: t('dataGovernance.quality.run.countsShort'),
      key: 'counts',
      render: (_, row) => `${row.passed}/${row.failed}/${row.error}`,
    },
    { title: t('dataGovernance.quality.run.executor'), dataIndex: 'executor', key: 'executor' },
    { title: t('dataGovernance.quality.run.createdAt'), dataIndex: 'createdAt', key: 'createdAt' },
    {
      title: t('dataGovernance.quality.run.actions'),
      key: 'actions',
      render: (_, row) => (
        <Button type="link" size="small" onClick={() => setDrawerRunId(row.id)}>
          {t('common.view')}
        </Button>
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
          placeholder={t('dataGovernance.quality.run.filterDataSource')}
          value={filterDataSourceId || undefined}
          style={{ width: 200 }}
          options={dataSourceOptions}
          onChange={(value) => { setFilterDataSourceId(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.run.status')}
          value={status || undefined}
          style={{ width: 150 }}
          options={[
            { value: 'running', label: t('dataGovernance.quality.runStatus.running') },
            { value: 'completed', label: t('dataGovernance.quality.runStatus.completed') },
            { value: 'failed', label: t('dataGovernance.quality.runStatus.failed') },
          ]}
          onChange={(value) => { setStatus(value ?? ''); setPage(1); }}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          {t('common.refresh')}
        </Button>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          disabled={!canWrite}
          onClick={() => void openCreate()}
        >
          {t('dataGovernance.quality.run.create')}
        </Button>
      </Space>

      {lastCreated ? (
        <Card
          size="small"
          style={{ marginBottom: 12 }}
          title={t('dataGovernance.quality.run.lastRunSql')}
          extra={(
            <Space>
              <Button
                size="small"
                icon={<CopyOutlined />}
                disabled={lastCreated.rules.length === 0}
                onClick={() => void handleCopyCreated()}
              >
                {t('dataGovernance.quality.run.copyAll')}
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                disabled={lastCreated.rules.length === 0}
                onClick={handleDownloadCreated}
              >
                {t('dataGovernance.quality.run.downloadSql')}
              </Button>
            </Space>
          )}
        >
          {lastCreated.rules.length > 0 ? (
            <pre className="yx-dg-quality-sql">
              {buildSqlText(lastCreated.rules, t('dataGovernance.quality.run.externalSql'))}
            </pre>
          ) : (
            <Empty description={t('dataGovernance.quality.run.snapshotEmpty')} />
          )}
        </Card>
      ) : null}

      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>}
        />
      ) : (
        <Table<DgQualityRun>
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={loading}
          size="middle"
          scroll={{ x: 900 }}
          locale={{ emptyText: loading ? null : <Empty description={t('dataGovernance.quality.empty')} /> }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            onChange: setPage,
            showTotal: (n) => t('dataGovernance.total', { total: n }),
          }}
        />
      )}

      <Modal
        open={createOpen}
        title={t('dataGovernance.quality.run.createTitle')}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void handleCreate()}
        okText={t('dataGovernance.quality.run.create')}
        width={760}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Select
            showSearch
            allowClear
            optionFilterProp="label"
            value={kbId || undefined}
            style={{ width: '100%' }}
            placeholder={t('dataGovernance.quality.run.selectKb')}
            options={kbOptions}
            onChange={(value) => void handleKbChange(value)}
          />
          <Select
            showSearch
            allowClear
            optionFilterProp="label"
            value={dataSourceId || undefined}
            style={{ width: '100%' }}
            disabled={!kbId}
            placeholder={t('dataGovernance.quality.run.selectDataSource')}
            options={createDataSourceOptions}
            onChange={(value) => void handleDataSourceChange(value)}
          />
          <Select
            mode="multiple"
            showSearch
            allowClear
            optionFilterProp="label"
            value={ruleIds}
            style={{ width: '100%' }}
            disabled={!dataSourceId}
            placeholder={t('dataGovernance.quality.run.selectRules')}
            options={ruleOptions}
            onChange={setRuleIds}
          />
          <Select
            value={dialect}
            style={{ width: 160 }}
            options={[
              { value: 'tsql', label: 'tsql' },
              { value: 'ansi', label: 'ansi' },
            ]}
            onChange={setDialect}
          />
          <Button onClick={() => void handlePreview()}>
            {t('dataGovernance.quality.run.preview')}
          </Button>
          {warnings.length > 0 ? (
            <Alert
              type="warning"
              showIcon
              message={warnings.slice(0, 5).join('; ')}
            />
          ) : null}
          {preview.length > 0 ? (
            <pre className="yx-dg-quality-sql">
              {buildSqlText(preview, t('dataGovernance.quality.run.externalSql'))}
            </pre>
          ) : null}
        </Space>
      </Modal>

      <QualityRunDrawer
        open={drawerRunId !== null}
        runId={drawerRunId}
        canWrite={canWrite}
        onClose={() => setDrawerRunId(null)}
        onChanged={() => void load()}
      />
    </div>
  );
}
