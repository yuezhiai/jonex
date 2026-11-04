import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Drawer, Empty, Input, Space, Spin, Table, Tag, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CopyOutlined, DownloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import {
  failQualityRun,
  getQualityRun,
  submitQualityRunResults,
} from '@/api/quality';
import type {
  DgQualityResultItem,
  DgQualityRuleSnapshotItem,
  DgQualityRunDetail,
} from '../types';
import {
  buildSqlText,
  formatMetricValue,
  severityColor,
  severityLabelKey,
  splitResultsJson,
  sqlFilename,
} from '../qualityUtils';

interface Props {
  open: boolean;
  runId: string | null;
  canWrite: boolean;
  onClose: () => void;
  onChanged: () => void;
}

export default function QualityRunDrawer({
  open, runId, canWrite, onClose, onChanged,
}: Props) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<DgQualityRunDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultsText, setResultsText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  // 快速 A→B 切换时，旧请求的响应不得覆盖新详情。
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    if (!runId) return;
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const next = await getQualityRun(runId);
      if (seq !== requestSeq.current) return;
      setDetail(next);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(err instanceof Error ? err.message : t('dataGovernance.quality.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [runId, t]);

  useEffect(() => {
    if (!open) return;
    setResultsText('');
    setDetail(null);
    setError(null);
    void load();
    return () => {
      // 关闭 / 切换 run 时作废在途请求。
      requestSeq.current += 1;
    };
  }, [open, load]);

  const handleSubmit = async () => {
    if (!runId || !canWrite) return;
    const parsed = splitResultsJson(resultsText);
    if (!parsed.ok) {
      message.error(t(`dataGovernance.quality.run.submitError.${parsed.error}`));
      return;
    }
    setSubmitting(true);
    try {
      await submitQualityRunResults(runId, { results: parsed.items });
      message.success(t('dataGovernance.quality.run.submitted'));
      setResultsText('');
      await load();
      onChanged();
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('dataGovernance.quality.run.submitFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleFail = async () => {
    if (!runId || !canWrite) return;
    const reason = window.prompt(t('dataGovernance.quality.run.failPrompt'));
    if (!reason?.trim()) return;
    try {
      await failQualityRun(runId, reason.trim());
      message.success(t('dataGovernance.quality.run.failed'));
      await load();
      onChanged();
    } catch (err) {
      message.error(err instanceof Error ? err.message : t('dataGovernance.quality.run.failFailed'));
    }
  };

  const snapshotSql = () => buildSqlText(
    (detail?.ruleSnapshot ?? []).map((item) => ({ name: item.name, sql: item.sql })),
    t('dataGovernance.quality.run.externalSql'),
  );

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(snapshotSql());
      message.success(t('dataGovernance.quality.run.copied'));
    } catch {
      message.error(t('dataGovernance.quality.run.copyFailed'));
    }
  };

  const handleDownload = () => {
    const blob = new Blob([snapshotSql()], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = sqlFilename(detail?.id ?? runId);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  };

  const resultColumns: ColumnsType<DgQualityResultItem> = [
    { title: t('dataGovernance.quality.run.ruleId'), dataIndex: 'ruleId', key: 'ruleId' },
    { title: t('dataGovernance.quality.run.asset'), dataIndex: 'assetId', key: 'assetId' },
    {
      title: t('dataGovernance.quality.run.result'),
      dataIndex: 'status',
      key: 'status',
      render: (value: string) => (
        <Tag color={value === 'passed' ? 'green' : value === 'failed' ? 'red' : 'orange'}>
          {t(`dataGovernance.quality.resultStatus.${value}`)}
        </Tag>
      ),
    },
    {
      title: t('dataGovernance.quality.run.metric'),
      dataIndex: 'metricValue',
      key: 'metricValue',
      render: (value: unknown) => formatMetricValue(value),
    },
    { title: t('dataGovernance.quality.run.message'), dataIndex: 'message', key: 'message', ellipsis: true },
  ];

  const snapshotColumns: ColumnsType<DgQualityRuleSnapshotItem> = [
    { title: t('dataGovernance.quality.run.ruleName'), dataIndex: 'name', key: 'name', ellipsis: true },
    {
      title: t('dataGovernance.quality.run.asset'),
      key: 'asset',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <span>{t(row.assetType === 'column'
            ? 'dataGovernance.quality.rule.column'
            : 'dataGovernance.quality.rule.table')}</span>
          <span className="yx-dg-quality-muted">{row.assetId}</span>
        </Space>
      ),
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
      title: t('dataGovernance.quality.run.sql'),
      dataIndex: 'sql',
      key: 'sql',
      render: (value: string | null) => (
        <pre className="yx-dg-quality-sql">{value ?? t('dataGovernance.quality.run.externalSql')}</pre>
      ),
    },
  ];

  return (
    <Drawer
      open={open}
      width={860}
      title={t('dataGovernance.quality.run.detailTitle')}
      onClose={onClose}
      destroyOnClose
    >
      {loading ? <Spin /> : null}
      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>}
        />
      ) : null}
      {detail ? (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Space wrap>
            <Tag color={detail.status === 'completed' ? 'green' : detail.status === 'failed' ? 'red' : 'processing'}>
              {t(`dataGovernance.quality.runStatus.${detail.status}`)}
            </Tag>
            <span>{detail.dataSourceId}</span>
            <span>
              {t('dataGovernance.quality.run.counts', {
                total: detail.total, passed: detail.passed,
                failed: detail.failed, error: detail.error,
              })}
            </span>
          </Space>
          {detail.status === 'running' && canWrite ? (
            <>
              <Input.TextArea
                rows={6}
                value={resultsText}
                placeholder={t('dataGovernance.quality.run.resultsPlaceholder')}
                onChange={(event) => setResultsText(event.target.value)}
              />
              <Space>
                <Button type="primary" loading={submitting} onClick={() => void handleSubmit()}>
                  {t('dataGovernance.quality.run.submit')}
                </Button>
                <Button danger onClick={() => void handleFail()}>
                  {t('dataGovernance.quality.run.fail')}
                </Button>
              </Space>
            </>
          ) : null}

          <div>
            <Space wrap style={{ marginBottom: 8 }}>
              <strong>{t('dataGovernance.quality.run.snapshotTitle')}</strong>
              <Button
                size="small"
                icon={<CopyOutlined />}
                disabled={detail.ruleSnapshot.length === 0}
                onClick={() => void handleCopy()}
              >
                {t('dataGovernance.quality.run.copyAll')}
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                disabled={detail.ruleSnapshot.length === 0}
                onClick={handleDownload}
              >
                {t('dataGovernance.quality.run.downloadSql')}
              </Button>
            </Space>
            <Table<DgQualityRuleSnapshotItem>
              rowKey="ruleId"
              columns={snapshotColumns}
              dataSource={detail.ruleSnapshot}
              size="small"
              pagination={false}
              locale={{ emptyText: <Empty description={t('dataGovernance.quality.run.snapshotEmpty')} /> }}
            />
          </div>

          <Table<DgQualityResultItem>
            rowKey={(row) => `${row.ruleId}:${row.assetId}`}
            columns={resultColumns}
            dataSource={detail.results}
            size="small"
            pagination={false}
            locale={{ emptyText: <Empty description={t('dataGovernance.quality.empty')} /> }}
          />
        </Space>
      ) : null}
    </Drawer>
  );
}
