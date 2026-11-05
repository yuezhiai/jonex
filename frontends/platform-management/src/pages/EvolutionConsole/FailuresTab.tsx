import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Descriptions, Empty, Select, Space, Spin, Table, Tag, Tooltip, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import {
  getLatestDiagnosis,
  listFailures,
  runDiagnosis,
  type DiagnosisProposal,
  type DiagnosisReport,
  type FailureRow,
  type SceneDigest,
} from '../../api/evolution';
import { askReasonLabel } from '../../utils/askReasons';
import MetricAdoptForm from './components/MetricAdoptForm';
import SqlPre from './components/SqlPre';

const DAY_OPTIONS = [7, 14, 30];

/** 现场 → 一行段列表（空段省略）。无 scene 时返回空数组（老报告兼容、不渲染新元素）。 */
export function sceneLines(scene: SceneDigest | undefined, t: (k: string) => string): string[] {
  if (!scene) return [];
  const segs: string[] = [];
  if (scene.anchor) segs.push(t('evolutionConsole.sceneAnchor') + '=' + scene.anchor);
  if (scene.multiturn === false) {
    segs.push(t('evolutionConsole.sceneSingleShot'));
  } else if (scene.tools && scene.tools.length > 0) {
    segs.push(t('evolutionConsole.sceneTools') + scene.tools.join('、'));
  }
  if (scene.truncated) segs.push(t('evolutionConsole.sceneTruncated'));
  if (scene.misses && scene.misses.length > 0) {
    segs.push(t('evolutionConsole.sceneMisses') + scene.misses.join('、'));
  }
  return segs;
}

const ATTRIBUTIONS: { key: string; labelKey: string; tipKey: string }[] = [
  { key: 'C', labelKey: 'evolutionConsole.bucketC', tipKey: 'evolutionConsole.bucketCTip' },
  { key: 'S', labelKey: 'evolutionConsole.bucketS', tipKey: 'evolutionConsole.bucketSTip' },
  { key: 'T', labelKey: 'evolutionConsole.bucketT', tipKey: 'evolutionConsole.bucketTTip' },
  { key: 'unknown', labelKey: 'evolutionConsole.bucketUnknown', tipKey: 'evolutionConsole.bucketUnknownTip' },
];

/** Tab1 失败诊断：失败样本清单（/failures）+ 生成诊断（/diagnosis）与报告渲染。 */
export default function FailuresTab() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [days, setDays] = useState(7);
  const [rows, setRows] = useState<FailureRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [diagnosing, setDiagnosing] = useState(false);
  const [report, setReport] = useState<DiagnosisReport | null>(null);
  const [reportPaths, setReportPaths] = useState<{ json: string | null; md: string | null }>({
    json: null, md: null,
  });

  // 乱序响应护栏：切天数/连点刷新时，只认最后一次请求的响应。
  const seqRef = useRef(0);

  const loadFailures = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await listFailures(days);
      if (seq !== seqRef.current) return;
      setRows(res.rows);
      setTotal(res.total);
    } catch (e: unknown) {
      if (seq !== seqRef.current) return;
      setRows([]);
      setTotal(0);
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [days, t]);

  useEffect(() => {
    loadFailures();
  }, [loadFailures]);

  // 进页时读一次最近诊断报告（不重跑）；读不到不影响失败清单。
  useEffect(() => {
    getLatestDiagnosis()
      .then((res) => {
        if (res.report) {
          setReport(res.report);
          setReportPaths({ json: res.json_path, md: res.md_path });
        }
      })
      .catch(() => undefined);
  }, []);

  const diagnose = async () => {
    setDiagnosing(true);
    try {
      const res = await runDiagnosis(days);
      if (res.ok && res.report) {
        setReport(res.report);
        setReportPaths({ json: res.json_path, md: res.md_path });
        message.success(t('evolutionConsole.diagnosisDone'));
      } else if (res.ok) {
        message.warning(t('evolutionConsole.diagnosisNoReport'));
      } else {
        message.error(t('evolutionConsole.diagnosisFailed', { rc: res.rc }));
      }
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('evolutionConsole.diagnosisRequestFailed'));
    } finally {
      setDiagnosing(false);
    }
  };

  const columns: ColumnsType<FailureRow> = [
    {
      title: t('evolutionConsole.failureTime'),
      dataIndex: 'created_at',
      width: 170,
      render: (v: string | null) => (v ? v.replace('T', ' ').slice(0, 19) : '-'),
    },
    {
      title: t('evolutionConsole.question'),
      dataIndex: 'question',
      ellipsis: true,
      render: (v: string | null) => v || '-',
    },
    {
      title: t('evolutionConsole.reasonCode'),
      dataIndex: 'reason_code',
      width: 260,
      render: (v: string | null) => (v ? <Tag color="red">{askReasonLabel(v)}</Tag> : '-'),
    },
    {
      title: t('evolutionConsole.resource'),
      dataIndex: 'resource_id',
      width: 300,
      ellipsis: true,
      render: (v: string | null) => v || '-',
    },
  ];

  const expandedRow = (row: FailureRow) => (
    <div style={{ padding: '0 8px' }}>
      {row.reason && (
        <p style={{ margin: '0 0 8px' }}>
          <b>{t('evolutionConsole.detailReason')}：</b>
          {row.reason}
        </p>
      )}
      {row.sql && (
        <div style={{ marginBottom: 8 }}>
          <b>{t('evolutionConsole.detailSql')}：</b>
          <SqlPre text={row.sql} />
        </div>
      )}
      {row.rejected_sql && (
        <div>
          <b>{t('evolutionConsole.detailRejectedSql')}：</b>
          <SqlPre text={row.rejected_sql} />
        </div>
      )}
      {/* 指标别名采纳：kb 取自行上字段。桥接 `/failures` 的 slim 行今日只有
          created_at/question/reason_code/reason/sql/rejected_sql/resource_id 七个字段
          （resource_id 是数据源 id），没有 kb 可从行内解析——故现阶段恒为 ''，
          由 MetricAdoptForm 的 `if (!kb) return null` 兜住不渲染；行上日后若带
          knowledge_base_id / kb_id 即自动点亮。 */}
      <MetricAdoptForm kb={(row as any).knowledge_base_id || (row as any).kb_id || ''} />
    </div>
  );

  const renderProposal = (p: DiagnosisProposal) => (
    <div
      key={p.id}
      style={{ border: '1px solid #f0f0f0', borderRadius: 8, padding: 12, marginBottom: 8 }}
    >
      <Space wrap>
        <Tag color="blue">{p.id}</Tag>
        <code>{p.sub_key}</code>
        {p.data_source_id && <Tag>{p.data_source_id}</Tag>}
        <Tag color="orange">{t('evolutionConsole.proposalCount', { n: p.count ?? 0 })}</Tag>
      </Space>
      <p style={{ margin: '8px 0 4px' }}>
        <b>{t('evolutionConsole.proposalAction')}：</b>
        {p.action}
      </p>
      {(p.samples ?? []).map((s, i) => (
        <div key={i} style={{ marginLeft: 8, marginBottom: 6 }}>
          <div>「{s.question}」</div>
          {s.sql && <SqlPre text={s.sql} />}
          {s.reason && <div style={{ color: '#8c8c8c', fontSize: 12 }}>{s.reason}</div>}
          {(() => {
            const segs = sceneLines(s.scene, t);
            const near = s.scene?.near_miss ?? [];
            if (segs.length === 0 && near.length === 0) return null;
            return (
              <div style={{ color: '#8c8c8c', fontSize: 12, marginTop: 2 }}>
                {segs.length > 0 && (
                  <div>{t('evolutionConsole.sceneLine')}{segs.join(' · ')}</div>
                )}
                {near.length > 0 && (
                  <div>{t('evolutionConsole.sceneNearMiss')}{near.join('、')}</div>
                )}
              </div>
            );
          })()}
        </div>
      ))}
    </div>
  );

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t('evolutionConsole.failureHint')}
        action={<a onClick={() => navigate('/operation-log')}>{t('evolutionConsole.goOperationLog')}</a>}
      />
      <Space style={{ marginBottom: 12 }} wrap>
        <span>{t('evolutionConsole.failuresTitle')}：</span>
        <Select
          value={days}
          style={{ width: 120 }}
          options={DAY_OPTIONS.map((d) => ({ value: d, label: t(`evolutionConsole.days${d}`) }))}
          onChange={(v) => setDays(v)}
        />
        {!error && <span>{t('evolutionConsole.failureTotal', { total })}</span>}
        <Button icon={<ReloadOutlined />} onClick={loadFailures}>
          {t('common.refresh')}
        </Button>
        <Button type="primary" loading={diagnosing} onClick={diagnose}>
          {diagnosing ? t('evolutionConsole.diagnosing') : t('evolutionConsole.diagnose')}
        </Button>
      </Space>

      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={loadFailures}>{t('common.retry')}</Button>}
        />
      ) : (
        <Table<FailureRow>
          rowKey={(r) => `${r.created_at}:${r.question ?? ''}`}
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          expandable={{ expandedRowRender: expandedRow }}
          locale={{ emptyText: <Empty description={t('evolutionConsole.failureEmpty')} /> }}
        />
      )}

      {report && (
        <div style={{ marginTop: 16 }}>
          <h3>{t('evolutionConsole.diagnosisTitle')}</h3>
          <Descriptions size="small" column={1} style={{ marginBottom: 8 }}>
            <Descriptions.Item label={t('evolutionConsole.diagnosisBuckets')}>
              <Space wrap>
                {ATTRIBUTIONS.map(({ key, labelKey, tipKey }) => (
                  <Tooltip key={key} title={t(tipKey)}>
                    <Tag>
                      {t(labelKey)}：{report.buckets?.[key as 'C' | 'S' | 'T' | 'unknown'] ?? 0}
                    </Tag>
                  </Tooltip>
                ))}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="window">
              {t('evolutionConsole.diagnosisWindow', {
                since: report.window?.since ?? '(不限)',
                until: report.window?.until ?? '-',
                total: report.window?.total ?? report.total ?? 0,
                skipped: report.window?.skipped ?? 0,
              })}
              {reportPaths.json && (
                <span style={{ marginLeft: 8, color: '#8c8c8c', fontSize: 12 }}>
                  {reportPaths.json}
                </span>
              )}
            </Descriptions.Item>
          </Descriptions>

          {(report.proposals ?? []).length === 0 && (
            <Alert type="success" showIcon message={t('evolutionConsole.proposalsEmpty')} />
          )}
          {ATTRIBUTIONS.map(({ key, labelKey, tipKey }) => {
            const items = (report.proposals ?? []).filter((p) => p.attribution === key);
            if (items.length === 0) return null;
            return (
              <div key={key} style={{ marginBottom: 12 }}>
                <Tooltip title={t(tipKey)}>
                  <h4>
                    {t(labelKey)}（{items.length}）
                  </h4>
                </Tooltip>
                {items.map(renderProposal)}
              </div>
            );
          })}

          {(report.rejected_proposals ?? []).length > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: 'pointer' }}>
                {t('evolutionConsole.rejectedTitle')}（{report.rejected_proposals?.length}）
              </summary>
              <div style={{ marginTop: 8 }}>
                {(report.rejected_proposals ?? []).map(renderProposal)}
              </div>
            </details>
          )}
        </div>
      )}

      {diagnosing && (
        <div style={{ textAlign: 'center', padding: 16 }}>
          <Spin />
        </div>
      )}
    </div>
  );
}
