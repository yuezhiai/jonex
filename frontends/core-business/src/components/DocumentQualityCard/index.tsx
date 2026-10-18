import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Empty, Space, Spin, Table, Tag, Tooltip } from 'antd';
import { ReloadOutlined, ExportOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import DocumentQualityBadge from '@/components/DocumentQualityBadge';
import { getDocumentQualityReport } from '@/api/domainKnowledge';
import type { DocumentQualityReport, QualityVerdict } from '@/types/domainKnowledge';

interface DocumentQualityCardProps {
  kbId: string;
  docId: string;
  /** 文档 updated_at：变化时重取（解析/重解析完成后刷新）。 */
  refreshKey?: string;
  canWrite?: boolean;
  onReparse?: () => void | Promise<void>;
}

type Metric = Record<string, unknown>;

function formatMetricValue(metric: Metric | undefined): string {
  if (!metric) return '—';
  const value = metric.value;
  // `null` = 空样本（没测），**不是 0** —— 显示「—」而不是「0」。
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  return String(value);
}

function metricRows(metrics: Record<string, unknown> | undefined) {
  if (!metrics) return [];
  return Object.entries(metrics)
    .filter(([, m]) => m && typeof m === 'object' && !Array.isArray(m))
    .map(([key, m]) => ({ key, metric: m as Metric }));
}

/**
 * 解析质量报告卡（单元二 D3）。
 *
 * ⚠ 渲染纪律（评审清单）：
 * - `attribution` **不展示**：`unknown` 是「没有命中判据、没有要归的因」，不是缺陷。
 * - `suggested_actions` 是**阶段契约**，渲染成按钮；它不代表「这份文档该修」。
 * - `verdict="ok"` 不是「全维度通过」：旁边同时显示「已标定 N 条判据未见命中」。
 * - 非支持链文档（`supported=false`）整卡不渲染。
 */
export default function DocumentQualityCard({
  kbId,
  docId,
  refreshKey,
  canWrite,
  onReparse,
}: DocumentQualityCardProps) {
  const { t } = useTranslation();
  const [data, setData] = useState<DocumentQualityReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!kbId || !docId) return;
    setLoading(true);
    setError(null);
    getDocumentQualityReport(kbId, docId)
      .then((result) => setData(result))
      .catch((err: any) =>
        setError(err?.message || t('domainKnowledge.quality.loadFailed')),
      )
      .finally(() => setLoading(false));
  }, [kbId, docId, t]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const report = data?.report as Record<string, any> | null | undefined;
  const sourceRows = useMemo(
    () => metricRows(report?.source as Record<string, unknown> | undefined),
    [report],
  );
  const chunkingRows = useMemo(
    () => metricRows(report?.chunking as Record<string, unknown> | undefined),
    [report],
  );

  if (loading && !data) {
    return (
      <div style={{ padding: '12px 0', textAlign: 'center' }}>
        <Spin tip={t('common.loading')} />
      </div>
    );
  }

  // 非支持链文档没有解析质量观测：整卡不渲染（不是「待评估」）。
  if (data && !data.supported) return null;

  const header = (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: 8,
      }}
    >
      <Space size={8}>
        <span style={{ fontSize: 13, fontWeight: 600, color: '#0b2b5c' }}>
          {t('domainKnowledge.quality.cardTitle')}
        </span>
        {/* 失败时**不显示**旧档位徽标：body 已变成网络错误提示，头部还挂一个
            「正常」会让用户以为质量正常、只是刷新失败（2026-10-18 真机发现）。 */}
        {!error && report && (
          <DocumentQualityBadge
            supported
            verdict={(report.verdict as QualityVerdict) ?? null}
            reasons={(report.reasons as string[]) || []}
          />
        )}
      </Space>
      <Tooltip title={t('domainKnowledge.quality.refresh')}>
        <Button
          type="text"
          size="small"
          icon={<ReloadOutlined />}
          onClick={load}
          loading={loading}
        />
      </Tooltip>
    </div>
  );

  if (error) {
    return (
      <div style={{ padding: '4px 0 12px' }}>
        {header}
        <Alert
          type="error"
          showIcon
          message={error}
          action={
            <Button size="small" onClick={load}>
              {t('domainKnowledge.quality.refresh')}
            </Button>
          }
        />
      </div>
    );
  }

  if (!data) return null;

  if (!report) {
    return (
      <div style={{ padding: '4px 0 12px' }}>
        {header}
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('domainKnowledge.quality.noReport')}
        />
      </div>
    );
  }

  const columns = [
    {
      title: t('domainKnowledge.quality.metric'),
      dataIndex: 'key',
      key: 'key',
      render: (key: string) =>
        t(`domainKnowledge.quality.dims.${key}`, { defaultValue: key }),
    },
    {
      title: t('domainKnowledge.quality.value'),
      key: 'value',
      width: 140,
      render: (_: unknown, row: { metric: Metric }) => formatMetricValue(row.metric),
    },
    {
      title: t('domainKnowledge.quality.checked'),
      key: 'checked',
      width: 110,
      render: (_: unknown, row: { metric: Metric }) =>
        `${row.metric.checked ?? 0} / ${row.metric.total ?? 0}`,
    },
  ];

  const emptyPages = Array.isArray(report.source?.empty_pages?.pages)
    ? (report.source.empty_pages.pages as number[])
    : [];
  const calibrated = Array.isArray(report.calibrated_rules)
    ? report.calibrated_rules.length
    : 0;
  const md = report.md as { digest?: string } | null | undefined;
  const mdUrl = data.md_url;
  const actions: string[] = Array.isArray(report.suggested_actions)
    ? report.suggested_actions
    : [];

  return (
    <div
      style={{
        border: '1px solid #e2e8f0',
        borderRadius: 8,
        padding: '12px 14px',
        marginBottom: 12,
        background: '#fff',
      }}
    >
      {header}

      {report.verdict === 'ok' && (
        <div style={{ fontSize: 12, color: '#64748b', marginBottom: 8 }}>
          {t('domainKnowledge.quality.calibrated', { count: calibrated })}
        </div>
      )}

      {Array.isArray(report.reasons) && report.reasons.length > 0 && (
        <Space size={6} wrap style={{ marginBottom: 8 }}>
          {(report.reasons as string[]).map((reason) => (
            <Tag key={reason} color="error" style={{ margin: 0 }}>
              {t(`domainKnowledge.quality.reasons.${reason}`, {
                defaultValue: reason,
              })}
            </Tag>
          ))}
        </Space>
      )}

      {emptyPages.length > 0 && (
        <div style={{ fontSize: 12, color: '#b45309', marginBottom: 8 }}>
          {t('domainKnowledge.quality.emptyPages')}：
          {emptyPages.map((p) => p + 1).join('、')}
        </div>
      )}

      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {sourceRows.length > 0 && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: '#475569', marginBottom: 4 }}>
              {t('domainKnowledge.quality.source')}
            </div>
            <Table
              size="small"
              pagination={false}
              rowKey="key"
              columns={columns}
              dataSource={sourceRows}
            />
          </div>
        )}
        {chunkingRows.length > 0 && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: '#475569', marginBottom: 4 }}>
              {t('domainKnowledge.quality.chunking')}
            </div>
            <Table
              size="small"
              pagination={false}
              rowKey="key"
              columns={columns}
              dataSource={chunkingRows}
            />
          </div>
        )}

        {(md?.digest || mdUrl) && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: '#475569', marginBottom: 4 }}>
              {t('domainKnowledge.quality.md')}
            </div>
            {md?.digest && (
              <pre
                style={{
                  margin: 0,
                  padding: 8,
                  background: '#f8fafc',
                  border: '1px solid #eef2f7',
                  borderRadius: 6,
                  fontSize: 12,
                  color: '#475569',
                  maxHeight: 160,
                  overflow: 'auto',
                  whiteSpace: 'pre-wrap',
                }}
              >
                {md.digest}
              </pre>
            )}
            {mdUrl && (
              <Button
                type="link"
                size="small"
                icon={<ExportOutlined />}
                href={mdUrl}
                target="_blank"
                rel="noreferrer"
                style={{ paddingLeft: 0 }}
              >
                {t('domainKnowledge.quality.openMd')}
              </Button>
            )}
          </div>
        )}

        {actions.length > 0 && (
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: '#475569', marginBottom: 4 }}>
              {t('domainKnowledge.quality.suggested')}
            </div>
            <Space size={8}>
              {actions.includes('reparse') && (
                <Button
                  size="small"
                  disabled={!canWrite}
                  onClick={() => onReparse?.()}
                >
                  {t('domainKnowledge.quality.reparse')}
                </Button>
              )}
            </Space>
          </div>
        )}

        {data.status && (
          <div style={{ fontSize: 11, color: '#94a3b8' }}>
            {t('domainKnowledge.quality.taskStatus')}：{data.status}
          </div>
        )}
      </Space>
    </div>
  );
}
