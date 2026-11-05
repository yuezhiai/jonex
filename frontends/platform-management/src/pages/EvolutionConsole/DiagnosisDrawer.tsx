import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Drawer, Space, Spin, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  getDislikeContext,
  replayDislike,
  type DislikeContext,
  type DislikeHistorySnap,
  type DislikeObservation,
  type ReplayResult,
} from '../../api/evolution';
import ConceptAdoptForm from './components/ConceptAdoptForm';
import FailedRowRepairCard from './components/FailedRowRepairCard';
import SqlPre from './components/SqlPre';
import { trackingUrl } from './constants';

interface Props {
  open: boolean;
  feedbackId: string | null;
  /** 列表行上的问句：context 未回前先有可读标题；回包后仍以行为准展示。 */
  query: string | null;
  onClose: () => void;
}

const fmtTime = (v: string | null) => (v ? v.replace('T', ' ').slice(0, 19) : '-');

/** HTTP 层错误的「尾巴」：信封 error_details 有货才展示（桥接 BridgeError 的 extra 走 data 段、通常没有）。 */
function errorTailOf(e: unknown): string {
  const det = (e as { errorDetails?: Record<string, unknown> } | null)?.errorDetails;
  if (!det || Object.keys(det).length === 0) return '';
  try {
    return JSON.stringify(det, null, 2);
  } catch {
    return String(det);
  }
}

/** 点踩「诊断」抽屉（第二层 a 的排障面）：现场（快照时间线 + 观测）→ 试跑（显式按钮，
 * 让 ② 以当前口径处理该问句）→ 归因（档位徽标 + 后端人话行动文案 + 跳转处理）。
 * 打开即拉现场；试跑只有点击才发请求（会真实调用模型）。 */
export default function DiagnosisDrawer({ open, feedbackId, query, onClose }: Props) {
  const { t } = useTranslation();
  const [ctx, setCtx] = useState<DislikeContext | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [replayRes, setReplayRes] = useState<ReplayResult | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [replayErrorTail, setReplayErrorTail] = useState('');
  /** 会话号：每次打开/换行 +1；在途响应落地前先确认还在同一会话（先例 EntryAdjustModal）。 */
  const sessionRef = useRef(0);

  // 打开/换行即重置（防跨行残留，2026-10-13 审查教训）：上一行的现场、试跑结果与迟到响应都不再认。
  // 先于下面的 load effect 声明：同一轮 effect 里会话号先 +1，load 捕获到的才是新会话。
  useEffect(() => {
    if (!open) return;
    sessionRef.current += 1;
    setCtx(null);
    setLoading(false);
    setError(null);
    setReplayRes(null);
    setReplaying(false);
    setReplayError(null);
    setReplayErrorTail('');
  }, [open, feedbackId]);

  const load = useCallback(async () => {
    if (!open || !feedbackId) return;
    const session = sessionRef.current; // 序号守卫：响应落地前先确认还在同一会话
    setLoading(true);
    setError(null);
    try {
      const data = await getDislikeContext(feedbackId);
      if (session !== sessionRef.current) return; // 期间关窗/换行：迟到响应丢弃
      setCtx(data);
    } catch (e: unknown) {
      if (session !== sessionRef.current) return;
      setError(e instanceof Error ? e.message : t('evolutionConsole.diagLoadFailed'));
    } finally {
      if (session === sessionRef.current) setLoading(false);
    }
  }, [open, feedbackId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const doReplay = useCallback(async () => {
    if (!feedbackId || replaying) return;
    const session = sessionRef.current;
    setReplaying(true);
    // 上一轮结果与错误先清：本次若失败，屏幕上只留本次的错误——旧归因不带标注留着会被误读为本次结果（2026-10-13 审查 Minor-2）。
    setReplayRes(null);
    setReplayError(null);
    setReplayErrorTail('');
    try {
      const res = await replayDislike(feedbackId);
      if (session !== sessionRef.current) return;
      setReplayRes(res);
    } catch (e: unknown) {
      if (session !== sessionRef.current) return;
      setReplayError(e instanceof Error ? e.message : t('evolutionConsole.diagReplayFailed'));
      setReplayErrorTail(errorTailOf(e));
    } finally {
      if (session === sessionRef.current) setReplaying(false);
    }
  }, [feedbackId, replaying, t]);

  const kbId = ctx?.feedback.kb_id ?? null;
  const hasAnchor = (ctx?.histories ?? []).some((h) => h.is_feedback_target);
  const failedRows = (replayRes?.replay.new_observations ?? []).filter(
    (o) => o.outcome === 'failed');

  const outcomeTag = (outcome: string | null) => {
    if (!outcome) return null;
    const color = outcome === 'success' ? 'success'
      : outcome === 'failed' ? 'error' : undefined;
    return (
      <Tag color={color}>
        {t(`evolutionConsole.dislikeOutcome.${outcome}`, { defaultValue: outcome })}
      </Tag>
    );
  };

  const historyCard = (h: DislikeHistorySnap, i: number) => (
    <div key={h.id ?? i} style={{ border: '1px solid #f0f0f0', borderRadius: 6,
                                  padding: '8px 10px', marginBottom: 8 }}>
      <Space size={6} wrap style={{ marginBottom: 6 }}>
        <span style={{ color: '#8c8c8c', fontSize: 12 }}>{fmtTime(h.created_at)}</span>
        {h.answer_len != null && (
          <span style={{ color: '#8c8c8c', fontSize: 12 }}>
            {t('evolutionConsole.diagHistoryLen', { n: h.answer_len })}
          </span>
        )}
        {h.is_feedback_target && <Tag color="blue">{t('evolutionConsole.diagAnchorTag')}</Tag>}
      </Space>
      {h.answer
        ? <SqlPre text={h.answer} maxHeight={160} />
        : <span style={{ color: '#8c8c8c', fontSize: 12 }}>—</span>}
    </div>
  );

  /** 观测行卡（现场段与试跑档共用；tail 槽给试跑档挂 data_source_id / row_count）。 */
  const obsCard = (o: DislikeObservation, i: number, tail?: React.ReactNode) => (
    <div key={i} style={{ border: '1px solid #f0f0f0', borderRadius: 6,
                          padding: '8px 10px', marginBottom: 8 }}>
      <Space size={6} wrap>
        <span style={{ color: '#8c8c8c', fontSize: 12 }}>{fmtTime(o.created_at)}</span>
        {outcomeTag(o.outcome)}
        {o.reason_code && <code>{o.reason_code}</code>}
        {o.anchor && <code>{o.anchor}</code>}
        {o.rounds && (
          <span style={{ color: '#8c8c8c', fontSize: 12 }}
                title={t('evolutionConsole.roundsHint')}>×{o.rounds}</span>
        )}
        {tail}
      </Space>
      {o.sql && <div style={{ marginTop: 6 }}><SqlPre text={o.sql} maxHeight={120} /></div>}
    </div>
  );

  /** 归因档徽标：特殊 kind 走产品档；其余按 attribution 档（C/S/T/unknown）。 */
  const attrBadge = (res: ReplayResult): { text: string; color?: string } => {
    const { kind, attribution } = res.attribution;
    if (kind === 'answered_ok') return { text: t('evolutionConsole.diagAnsweredYes'), color: 'success' };
    if (kind === 'empty_result') return { text: t('evolutionConsole.diagEmptyResult'), color: 'gold' };
    if (kind === 'no_observation') return { text: t('evolutionConsole.diagNoObservation') };
    if (attribution === 'C') return { text: t('evolutionConsole.diagAttrC'), color: 'blue' };
    if (attribution === 'S') return { text: t('evolutionConsole.diagAttrS'), color: 'orange' };
    if (attribution === 'T') return { text: t('evolutionConsole.diagAttrT'), color: 'purple' };
    return { text: t('evolutionConsole.diagAttrUnknown') };
  };

  const answeredTag = (answered: boolean | null) => {
    if (answered === true) return <Tag color="success">{t('evolutionConsole.diagAnsweredYes')}</Tag>;
    if (answered === false) return <Tag color="orange">{t('evolutionConsole.diagAnsweredNo')}</Tag>;
    return <Tag>{t('evolutionConsole.diagAnsweredNone')}</Tag>;
  };

  return (
    <Drawer open={open} onClose={onClose} title={t('evolutionConsole.diagTitle')} size={720}>
      <p style={{ margin: '0 0 12px' }}>
        <b>{t('evolutionConsole.question')}：</b>
        {(query ?? ctx?.feedback.query) || '-'}
      </p>

      {/* ── 现场：检索快照时间线 + ② 观测（四态：loading / error 重试 / 成功 / 空态兜底） ── */}
      <div style={{ marginBottom: 16 }}>
        <b style={{ display: 'block', marginBottom: 8 }}>{t('evolutionConsole.diagSceneTitle')}</b>
        {loading && !ctx ? (
          <div style={{ textAlign: 'center', padding: 24 }}><Spin /></div>
        ) : error && !ctx ? (
          <Alert type="error" showIcon message={error}
                 action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>} />
        ) : ctx ? (
          <>
            <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>
              {t('evolutionConsole.diagHistoriesTitle')}
            </div>
            {ctx.histories.length === 0 ? (
              <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 12px' }}>
                {t('evolutionConsole.diagHistoriesEmpty')}
              </p>
            ) : (
              <>
                <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
                  {t('evolutionConsole.diagHistoryHint')}
                </p>
                {ctx.histories.map(historyCard)}
                {!hasAnchor && (
                  <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 12px' }}>
                    {t('evolutionConsole.diagNoAnchorHint')}
                  </p>
                )}
              </>
            )}
            <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>
              {t('evolutionConsole.diagObservationsTitle')}
            </div>
            {ctx.observations.length === 0 ? (
              <p style={{ color: '#8c8c8c', fontSize: 12, margin: 0 }}>
                {t('evolutionConsole.diagObservationsEmpty')}
              </p>
            ) : (
              ctx.observations.map((o, i) => obsCard(o, i))
            )}
          </>
        ) : null}
      </div>

      {/* ── 试跑：显式按钮（点击才有请求；会调用模型） ── */}
      <div style={{ marginBottom: 16 }}>
        <b style={{ display: 'block', marginBottom: 8 }}>{t('evolutionConsole.diagReplayTitle')}</b>
        <Button type="primary" loading={replaying} disabled={!feedbackId}
                onClick={() => void doReplay()}>
          {replaying ? t('evolutionConsole.diagReplaying') : t('evolutionConsole.diagReplayBtn')}
        </Button>
        <p style={{ color: '#8c8c8c', fontSize: 12, margin: '8px 0 0' }}>
          {t('evolutionConsole.diagReplayHint')}
        </p>
        {replayError && (
          <Alert type="error" showIcon style={{ marginTop: 8 }} message={replayError}
                 description={replayErrorTail
                   ? <SqlPre text={replayErrorTail} maxHeight={160} />
                   : undefined} />
        )}
        {replayRes && (
          <div style={{ marginTop: 12 }}>
            <Space size={6} wrap style={{ marginBottom: 8 }}>
              {answeredTag(replayRes.replay.answered)}
              {replayRes.replay.source && (
                <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                  {t('evolutionConsole.diagReplaySource')}：{replayRes.replay.source}
                </span>
              )}
            </Space>
            {replayRes.replay.answer_snippet
              ? <SqlPre text={replayRes.replay.answer_snippet} maxHeight={160} />
              : <span style={{ color: '#8c8c8c', fontSize: 12 }}>—</span>}
            {replayRes.replay.new_observations.length > 0 && (
              <div style={{ marginTop: 8 }}>
                <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 4 }}>
                  {t('evolutionConsole.diagNewObservations')}
                </div>
                {replayRes.replay.new_observations.map((r, i) =>
                  obsCard(r, i, (
                    <React.Fragment>
                      {r.data_source_id != null && <code>{r.data_source_id}</code>}
                      <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                        {t('evolutionConsole.diagRowCount', { n: r.row_count ?? '—' })}
                      </span>
                    </React.Fragment>
                  )))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── 归因：试跑后出现（档位徽标 + 行动文案原样 + 跳转处理） ── */}
      {replayRes && (
        <div>
          <b style={{ display: 'block', marginBottom: 8 }}>{t('evolutionConsole.diagAttrTitle')}</b>
          <Space size={6} wrap style={{ marginBottom: 8 }}>
            <Tag color={attrBadge(replayRes).color}>{attrBadge(replayRes).text}</Tag>
          </Space>
          <p style={{ margin: '0 0 8px' }}>{replayRes.attribution.action}</p>
          {['no_observation', 'concept_gap'].includes(replayRes.attribution.kind)
            && kbId && feedbackId && (
            <div style={{ margin: '0 0 8px' }}>
              <ConceptAdoptForm kb={kbId} feedbackId={feedbackId} />
            </div>
          )}
          {!['no_observation', 'concept_gap'].includes(replayRes.attribution.kind)
            && kbId && feedbackId && failedRows.length > 0 && (
            <div style={{ margin: '0 0 8px' }}>
              <b style={{ display: 'block', marginBottom: 8 }}>
                {t('evolutionConsole.failedRowRepairTitle')}
              </b>
              {failedRows.map((r, i) => (
                <FailedRowRepairCard key={`${r.data_source_id ?? 'x'}-${i}`}
                                     row={r} kb={kbId} feedbackId={feedbackId} />
              ))}
            </div>
          )}
          {kbId && (
            <Button type="link" size="small" style={{ paddingLeft: 0 }}
                    onClick={() => {
                      // 与 DislikesTab 同款：dev 下子应用以同源 iframe（standalone）挂载，
                      // 必须走顶层 location，否则只导航 iframe 自己（壳套壳）。
                      const target = window.top?.location ?? window.location;
                      target.href = trackingUrl(kbId);
                    }}>
              {t('evolutionConsole.dislikeHandle')}
            </Button>
          )}
        </div>
      )}
    </Drawer>
  );
}
