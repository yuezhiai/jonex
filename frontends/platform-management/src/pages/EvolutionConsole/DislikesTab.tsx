import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Radio, Space, Spin, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getDislikes, type DislikeRow } from '../../api/evolution';
import DiagnosisDrawer from './DiagnosisDrawer';
import { trackingUrl } from './constants';

export default function DislikesTab() {
  const { t } = useTranslation();
  const [days, setDays] = useState(30);
  const [rows, setRows] = useState<DislikeRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** 诊断抽屉的目标行（null = 关）；抽屉内部按 feedbackId 拉现场。 */
  const [diagTarget, setDiagTarget] = useState<DislikeRow | null>(null);

  // 乱序响应护栏：切天数/连点刷新时，只认最后一次请求的响应。
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError(null);
    try {
      const res = await getDislikes(days);
      if (seq !== seqRef.current) return;
      setRows(res.rows);
    } catch (e: unknown) {
      if (seq !== seqRef.current) return;
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [days, t]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !rows) {
    return <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>;
  }
  if (error && !rows) {
    return (
      <Alert type="error" showIcon message={error}
             action={<Button size="small" onClick={load}>{t('common.retry')}</Button>} />
    );
  }
  if (!rows) return null;

  const columns: ColumnsType<DislikeRow> = [
    { title: t('evolutionConsole.failureTime'), dataIndex: 'created_at', width: 170,
      render: (v: string) => (v ? v.replace('T', ' ').slice(0, 19) : '-') },
    { title: t('evolutionConsole.question'), dataIndex: 'query', ellipsis: true,
      render: (v: string | null) => v || '-' },
    { title: t('evolutionConsole.dislikeReason'), dataIndex: 'feedback_reason', width: 130,
      render: (v: string | null) => (v
        ? t(`evolutionConsole.feedbackReason.${v}`, { defaultValue: v })
        : '-') },
    { title: t('evolutionConsole.dislikeComment'), dataIndex: 'feedback_comment', width: 160,
      ellipsis: true, render: (v: string | null) => v || '-' },
    { title: t('evolutionConsole.dislikeScene'), key: 'scene', width: 200,
      render: (_, r) => (r.anchor || r.outcome ? (
        <Space size={4} wrap>
          {r.anchor ? <code>{r.anchor}</code> : <span>—</span>}
          {r.outcome && (
            <Tag color={r.outcome === 'success' ? 'success' : 'error'}>
              {t(`evolutionConsole.dislikeOutcome.${r.outcome}`, { defaultValue: r.outcome })}
            </Tag>
          )}
          {r.rounds && (
            <span style={{ color: '#8c8c8c', fontSize: 12 }}
                  title={t('evolutionConsole.roundsHint')}>×{r.rounds}</span>
          )}
        </Space>
      ) : '—') },
    { title: t('evolutionConsole.dislikeAdopted'), dataIndex: 'adopted', width: 90,
      render: (v: boolean) => (v
        ? <Tag color="success">{t('evolutionConsole.dislikeAdoptedYes')}</Tag>
        : <Tag>{t('evolutionConsole.dislikeAdoptedNo')}</Tag>) },
    { title: t('evolutionConsole.dislikeAction'), key: 'action', width: 150,
      render: (_, r) => {
        const kbId = r.kb_id;
        return (
          <Space size={2}>
            <Button size="small" type="link" onClick={() => setDiagTarget(r)}>
              {t('evolutionConsole.diagAction')}
            </Button>
            {kbId ? (
              <Tooltip title={t('evolutionConsole.dislikeActionHint')}>
                <Button size="small" type="link"
                        onClick={() => {
                          // dev 下子应用以同源 iframe（standalone）挂载：必须走顶层 location，
                          // 否则只导航 iframe 自己（壳套壳）。
                          const target = window.top?.location ?? window.location;
                          target.href = trackingUrl(kbId);
                        }}>
                  {t('evolutionConsole.dislikeHandle')}
                </Button>
              </Tooltip>
            ) : (
              <Tooltip title={t('evolutionConsole.dislikeNoKb')}>
                <span style={{ color: '#bfbfbf' }}>—</span>
              </Tooltip>
            )}
          </Space>
        );
      } },
  ];

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
             message={t('evolutionConsole.dislikesHint')} />
      {error && rows && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={error}
               action={<Button size="small" onClick={load}>{t('common.retry')}</Button>} />
      )}
      <Space style={{ marginBottom: 12 }}>
        <Radio.Group value={days} onChange={(e) => setDays(e.target.value)} size="small">
          <Radio.Button value={7}>{t('evolutionConsole.days7')}</Radio.Button>
          <Radio.Button value={30}>{t('evolutionConsole.days30')}</Radio.Button>
        </Radio.Group>
        <Button size="small" icon={<ReloadOutlined />} onClick={load} loading={loading}>
          {t('common.refresh')}
        </Button>
      </Space>
      {rows.length === 0
        ? <Empty description={t('evolutionConsole.dislikesEmpty')} />
        : <Table<DislikeRow> rowKey="id" size="small" loading={loading}
                              pagination={{ pageSize: 20 }}
                              columns={columns} dataSource={rows} scroll={{ x: 1160 }} />}

      {/* 诊断抽屉铺在组件尾（照 RunDetail 的 EntryAdjustModal 装配方式） */}
      <DiagnosisDrawer
        open={diagTarget !== null}
        feedbackId={diagTarget?.id ?? null}
        query={diagTarget?.query ?? null}
        onClose={() => setDiagTarget(null)}
      />
    </div>
  );
}
