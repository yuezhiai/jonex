import React from 'react';
import { useTranslation } from 'react-i18next';
import { Card, Button, Popconfirm, Radio, Input, Spin, message } from 'antd';
import {
  SearchOutlined,
  StopOutlined,
  ReloadOutlined,
  CloseCircleOutlined,
  CaretDownOutlined,
  CaretRightOutlined,
  NodeIndexOutlined,
  BulbOutlined,
  FileTextOutlined,
  PictureOutlined,
  LikeOutlined,
  LikeFilled,
  DislikeOutlined,
  DislikeFilled,
  LoadingOutlined,
} from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import MarkdownContent from '@/components/MarkdownContent';
import type {
  KnowledgeReference,
  KnowledgeReferenceLocation,
  ReasoningStep,
  SearchFeedbackType,
  FeedbackReason,
} from '@/types/knowledgeSearch';
import type { OntologyInstanceRow } from '@/types/domainKnowledge';
import type { SearchSession } from './index';
import { getChunkDetail } from '@/api/domainKnowledge';
import OntologyEntityDrawer from './OntologyEntityDrawer';
import RagRecallDrawer, { type RagRecallItem } from './RagRecallDrawer';
// ── Helper: 时间戳格式化 ──
function formatTimestamp(sec?: number | null): string {
  if (sec == null || Number.isNaN(sec)) return '';
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
}

// ── Helper: 媒体类型标签 ──
function getMediaLabel(type: string, t: (key: string) => string): string {
  const map: Record<string, string> = {
    text: 'knowledgeSearch.mediaTypeText',
    pdf: 'knowledgeSearch.mediaTypePdf',
    audio: 'knowledgeSearch.mediaTypeAudio',
    video: 'knowledgeSearch.mediaTypeVideo',
    image: 'knowledgeSearch.mediaTypeImage',
  };
  return t(map[type] || 'knowledgeSearch.mediaTypeOther');
}

// ── [jonex] §image-refs P3-2: 图片资产（answer 图片条 / 卡片缩略图共用） ──
interface ImageAsset {
  docId: string;
  imageIdx: number;
  assetUrl: string;
  pageNo?: number | null;
  description?: string;
}

/** 从引用集合提取图片资产：筛 locations[].type === 'image' 且 asset_url
 *  非空，按 (doc_id, image_idx) 去重；描述文本剥「图片描述：」前缀。
 *  与答案同源（_rag_platform_answer 的 references 与 prompt 同源，见
 *  image-reference-chain-execution-plan.md P3 设计决策）。 */
function collectImageAssets(references?: KnowledgeReference[] | null): ImageAsset[] {
  const seen = new Set<string>();
  const out: ImageAsset[] = [];
  for (const ref of references || []) {
    for (const loc of ref.locations || []) {
      if (loc.type !== 'image' || !loc.asset_url || loc.image_idx == null) continue;
      const key = `${ref.doc_id}:${loc.image_idx}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        docId: ref.doc_id,
        imageIdx: loc.image_idx,
        assetUrl: loc.asset_url,
        pageNo: loc.page_no,
        description: (loc.text || '').replace(/^图片描述：/, '').trim(),
      });
    }
  }
  return out;
}

// ── [jonex] §image-refs P3-2: 单张图片资产缩略图（loading 骨架 / 失败降级 / 页码角标） ──
function ImageAssetThumb({
  asset,
  compact = false,
  t,
  onOpen,
}: {
  asset: ImageAsset;
  compact?: boolean;
  t: (key: string, opts?: any) => string;
  onOpen?: (asset: ImageAsset) => void;
}) {
  const [state, setState] = React.useState<'loading' | 'ok' | 'error'>('loading');
  const size = compact ? 72 : 96;
  return (
    <div
      style={{
        width: size,
        flexShrink: 0,
        border: '1px solid #e8edf5',
        borderRadius: 8,
        overflow: 'hidden',
        background: '#fff',
        cursor: state === 'ok' ? 'pointer' : 'default',
      }}
      title={asset.description || undefined}
    >
      <div
        style={{
          width: size,
          height: size,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          position: 'relative',
        }}
        onClick={state === 'ok' ? () => onOpen?.(asset) : undefined}
      >
        {state === 'loading' && <Spin size="small" />}
        {state !== 'error' && (
          <img
            src={asset.assetUrl}
            alt={asset.description || `img_${asset.imageIdx}`}
            style={{
              width: '100%',
              height: '100%',
              objectFit: 'cover',
              position: 'absolute',
              inset: 0,
              opacity: state === 'ok' ? 1 : 0,
            }}
            onLoad={() => setState('ok')}
            onError={() => setState('error')}
          />
        )}
        {state === 'error' && (
          <div style={{ fontSize: 11, color: '#94a3b8', textAlign: 'center', padding: '0 6px', lineHeight: 1.5 }}>
            {t('knowledgeSearch.imageLoadFailed')}
          </div>
        )}
        {state === 'ok' && asset.pageNo != null && (
          <span
            style={{
              position: 'absolute',
              bottom: 3,
              right: 3,
              fontSize: 10,
              color: '#fff',
              background: 'rgba(15,23,42,0.55)',
              borderRadius: 3,
              padding: '0 4px',
              lineHeight: 1.6,
            }}
          >
            {/* [jonex] 图片引用准确性修复：page_no 全链路 0-based（MinerU page_idx
                原样透传），展示层 +1 对齐用户认知的 PDF 真实页码 */}
            {t('knowledgeSearch.imagePage', { page: asset.pageNo + 1 })}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Helper: 推理链状态元 ──
function reasoningStatusMeta(status: string, t: (key: string) => string): { color: string; bg: string; label: string } {
  switch (status) {
    case 'done':
      return { color: '#10b981', bg: '#ecfdf5', label: t('status.done') };
    case 'skipped':
      return { color: '#94a3b8', bg: '#f1f5f9', label: t('status.skipped') };
    case 'failed':
      return { color: '#ef4444', bg: '#fef2f2', label: t('status.failed') };
    case 'running':
      return { color: '#3b82f6', bg: '#eff6ff', label: t('status.running') };
    default:
      return { color: '#64748b', bg: '#f1f5f9', label: status };
  }
}

// ── Helper: 数值安全格式化 ──
function fmtScore(v: unknown): string {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n.toFixed(2) : String(v ?? '');
}

// ── 推理状态小标签 ──
function ReasoningChip({
  children,
  tone = 'default',
}: {
  children: React.ReactNode;
  tone?: 'default' | 'primary' | 'success' | 'danger';
}) {
  const tones = {
    default: { bg: '#f1f5f9', color: '#64748b' },
    primary: { bg: '#f5f3ff', color: '#7c3aed' },
    success: { bg: '#ecfdf5', color: '#10b981' },
    danger: { bg: '#fef2f2', color: '#ef4444' },
  } as const;
  const t = tones[tone];
  return (
    <span
      style={{
        fontSize: 11,
        background: t.bg,
        color: t.color,
        borderRadius: 4,
        padding: '1px 7px',
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  );
}

// ── 推理步骤详情 ──
function StepDetail({
  step,
  t,
  onEntityClick,
  onRecallClick,
  references,
  onOpenReference,
}: {
  step: any;
  t: (key: string, opts?: any) => string;
  onEntityClick?: (hit: OntologyInstanceRow & { score?: number; kb_id?: string }) => void;
  onRecallClick?: (item: RagRecallItem, rank: number, score?: number | null) => void;
  references?: any[];
  onOpenReference?: (ref: any) => void;
}) {
  if (!step.detail) return null;
  const d = step.detail as Record<string, any>;

  const wrap = (children: React.ReactNode) => (
    <div
      style={{ marginTop: 8, background: '#fff', border: '1px solid #efeafc', borderRadius: 8, padding: '8px 10px' }}
    >
      {children}
    </div>
  );

  switch (step.stage) {
    case 'ontology_match': {
      const hits: any[] = Array.isArray(d.hits) ? d.hits : [];
      if (hits.length === 0) return null;
      return wrap(
        <>
          <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
            {t('knowledgeSearch.hitEntities', {
              count: hits.length,
              total: d.total_hits ?? hits.length,
              kbCount: d.kb_count ?? '—',
            })}
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {hits.map((h, idx) => (
              <Button
                key={idx}
                size="small"
                type="default"
                onClick={() => onEntityClick?.(h)}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  fontSize: 12,
                  background: '#f7f5fe',
                  borderColor: '#ece9fb',
                  borderRadius: 6,
                  padding: '2px 8px',
                  height: 'auto',
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = '#efeafc')}
                onMouseLeave={(e) => (e.currentTarget.style.background = '#f7f5fe')}
              >
                <span style={{ fontWeight: 600, color: '#4c1d95' }}>{h.name}</span>
                <span style={{ fontSize: 11, color: '#a78bda' }}>score {fmtScore(h.score)}</span>
                {h.kb_id && <span style={{ fontSize: 10, color: '#b6bdc7' }}>{h.kb_id}</span>}
              </Button>
            ))}
          </div>
        </>,
      );
    }
    case 'route_decision': {
      const route = d.route;
      const routeLabel =
        route === 'ontology'
          ? t('knowledgeSearch.ontologyRoute')
          : route === 'rag'
            ? t('knowledgeSearch.ragRoute')
            : String(route ?? '—');
      return wrap(
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
          <ReasoningChip tone="primary">{t('knowledgeSearch.routeTo', { route: routeLabel })}</ReasoningChip>
          {d.ft_score != null && (
            <ReasoningChip>
              {t('knowledgeSearch.ftScore', { score: fmtScore(d.ft_score), threshold: fmtScore(d.ftscore_threshold) })}
            </ReasoningChip>
          )}
          {d.vscore != null && (
            <ReasoningChip>
              {t('knowledgeSearch.vscore', { score: fmtScore(d.vscore), threshold: fmtScore(d.vscore_threshold) })}
            </ReasoningChip>
          )}
        </div>,
      );
    }
    case 'fact_lookup': {
      return wrap(
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
          {d.entity && <ReasoningChip tone="primary">{t('knowledgeSearch.entity', { name: d.entity })}</ReasoningChip>}
          {d.fact_count != null && (
            <ReasoningChip tone="success">{t('knowledgeSearch.factsCount', { count: d.fact_count })}</ReasoningChip>
          )}
          {d.kb_id && <ReasoningChip>{d.kb_id}</ReasoningChip>}
        </div>,
      );
    }
    case 'rag_fallback': {
      const ok: string[] = Array.isArray(d.kb_ok) ? d.kb_ok : [];
      const failed: string[] = Array.isArray(d.kb_failed) ? d.kb_failed : [];
      const recalls: RagRecallItem[] = Array.isArray(d.recalls) ? d.recalls : [];
      const scores: number[] = Array.isArray(d.p1_5?.prellm_rerank_top_scores)
        ? d.p1_5.prellm_rerank_top_scores
        : [];
      const hasContent = ok.length > 0 || failed.length > 0 || recalls.length > 0;
      if (!hasContent) return null;
      return wrap(
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {ok.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('knowledgeSearch.hitKbs')}</span>
              {ok.map((k) => (
                <ReasoningChip key={k} tone="success">
                  {k}
                </ReasoningChip>
              ))}
            </div>
          )}
          {failed.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('knowledgeSearch.failedKbs')}</span>
              {failed.map((k) => (
                <ReasoningChip key={k} tone="danger">
                  {k}
                </ReasoningChip>
              ))}
            </div>
          )}
          {recalls.length > 0 && (
            <div>
              <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
                {t('knowledgeSearch.recallChunks')}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {recalls.map((item, idx) => {
                  const rank = idx + 1;
                  const score = item.score ?? scores[idx];
                  return (
                    <Button
                      key={item.chunk_id || idx}
                      size="small"
                      type="default"
                      onClick={() => onRecallClick?.(item, rank, score ?? null)}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 6,
                        fontSize: 12,
                        background: '#f0f9ff',
                        borderColor: '#bae6fd',
                        borderRadius: 6,
                        padding: '2px 8px',
                        height: 'auto',
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = '#e0f2fe')}
                      onMouseLeave={(e) => (e.currentTarget.style.background = '#f0f9ff')}
                    >
                      <span style={{ fontWeight: 600, color: '#0369a1' }}>
                        {t('knowledgeSearch.chunkTag', { rank: String(rank).padStart(3, '0') })}
                      </span>
                      <span style={{ fontSize: 11, color: '#38bdf8' }}>
                        {score != null ? `${t('knowledgeSearch.relevanceShort')} ${fmtScore(score)}` : ''}
                      </span>
                    </Button>
                  );
                })}
              </div>
            </div>
          )}
        </div>,
      );
    }
    case 'openkb_query': {
      const turns: any[] = Array.isArray(d.turns) ? d.turns : [];
      // [jonex] 构建 wiki_path → reference 索引，供路径可点开
      const refByPath: Record<string, any> = {};
      if (references && onOpenReference) {
        for (const r of references) {
          if (r.wiki_path) refByPath[r.wiki_path] = r;
        }
      }
      const renderCall = (c: any, idx: number) => {
        const wikiPath: string = c.args?.path || '';
        const ref = wikiPath ? refByPath[wikiPath] : undefined;
        const canOpen = Boolean(ref && onOpenReference);
        return (
          <div key={idx} style={{ display: 'flex', alignItems: 'baseline', gap: 6, fontSize: 12 }}>
            <ReasoningChip tone="primary">{c.tool}</ReasoningChip>
            {canOpen ? (
              <a
                onClick={(e) => { e.preventDefault(); onOpenReference!(ref); }}
                style={{ color: '#4c1d95', cursor: 'pointer', wordBreak: 'break-all' as const, textDecoration: 'underline' }}
              >
                {wikiPath}
              </a>
            ) : (
              <span style={{ color: '#4c1d95', wordBreak: 'break-all' as const }}>
                {c.args?.path || c.args?.doc_name || c.args?.image_path || '—'}
              </span>
            )}
            {c.output_chars != null && (
              <span style={{ fontSize: 10, color: '#b6bdc7' }}>{c.output_chars} chars</span>
            )}
          </div>
        );
      };
      if (turns.length === 0) {
        // fallback to flat tool_calls for backward compat
        const flat: any[] = Array.isArray(d.tool_calls) ? d.tool_calls : [];
        if (flat.length === 0) return null;
        return wrap(
          <>
            <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
              {t('knowledgeSearch.wikiBrowsed', { count: flat.length })}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 240, overflowY: 'auto' as const }}>
              {flat.map((c, idx) => renderCall(c, idx))}
            </div>
            {d.tool_calls_truncated && (
              <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4 }}>
                {t('knowledgeSearch.wikiTruncated', { shown: flat.length, total: d.tool_calls_total })}
              </div>
            )}
          </>,
        );
      }
      return wrap(
        <>
          <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
            {t('knowledgeSearch.wikiTurns', { turns: turns.length, calls: d.tool_calls_total })}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 320, overflowY: 'auto' as const }}>
            {turns.map((turn: any, ti: number) => (
              <div key={ti}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginBottom: turn.calls?.length ? 2 : 0 }}>
                  <ReasoningChip tone="primary">
                    {/* [jonex] 后端 turn.index 从 0 开始（len(turns) 递增），
                        展示"第几轮"需 +1；无 index 时用数组位置 ti 兜底同样 +1。
                        注意不能写 turn.index ?? ti + 1——那会在有 index 时显示 0 起始。 */}
                    {t('knowledgeSearch.wikiTurn', { index: (turn.index ?? ti) + 1 })}
                  </ReasoningChip>
                  {turn.llm_ms != null && (
                    <span style={{ fontSize: 10, color: '#b6bdc7' }}>
                      {turn.llm_ms >= 1000
                        ? `${(turn.llm_ms / 1000).toFixed(1)}s`
                        : `${turn.llm_ms}ms`}
                    </span>
                  )}
                </div>
                {turn.thinking && (
                  <div
                    style={{ fontSize: 11, color: '#64748b', marginBottom: turn.calls?.length ? 4 : 0, lineHeight: 1.5 }}
                    title={turn.thinking}
                  >
                    {turn.thinking.length > 120 ? turn.thinking.slice(0, 120) + '…' : turn.thinking}
                  </div>
                )}
                {(turn.calls || []).map((c: any, ci: number) => renderCall(c, ci))}
              </div>
            ))}
          </div>
          {d.tool_calls_truncated && (
            <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4 }}>
              {t('knowledgeSearch.wikiTruncated', { shown: turns.reduce((s: number, t: any) => s + (t.calls?.length || 0), 0), total: d.tool_calls_total })}
            </div>
          )}
        </>,
      );
    }
    default:
      return null;
  }
}

// ── 反馈按钮 ──
const FEEDBACK_REASONS: FeedbackReason[] = [
  'inaccurate',
  'not_answered',
  'incomplete',
  'wrong_reference',
  'missing_knowledge',
  'other',
];

function FeedbackButtons({
  activeVote,
  loading,
  onVote,
  dislikeOpen,
  voteReason,
  voteComment,
  onDislikeOpenChange,
  onVoteReasonChange,
  onVoteCommentChange,
  onDislikeConfirm,
}: {
  activeVote: SearchFeedbackType | null;
  loading: SearchFeedbackType | null;
  onVote: (type: SearchFeedbackType) => void;
  dislikeOpen: boolean;
  voteReason: FeedbackReason | null;
  voteComment: string;
  onDislikeOpenChange: (open: boolean) => void;
  onVoteReasonChange: (reason: FeedbackReason) => void;
  onVoteCommentChange: (comment: string) => void;
  onDislikeConfirm: () => void;
}) {
  const { t } = useTranslation();
  const isLikeLoading = loading === 'like';
  const isDislikeLoading = loading === 'dislike';
  const isLikeSelected = activeVote === 'like';
  const isDislikeSelected = activeVote === 'dislike';

  return (
    <div
      style={{
        marginTop: 14,
        paddingTop: 12,
        borderTop: '1px solid #e8edf5',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        fontSize: 13,
        color: '#94a3b8',
      }}
    >
      <span style={{ color: '#94a3b8' }}>{t('knowledgeSearch.helpfulQuestion')}</span>

      {/* 有帮助 */}
      <button
        type="button"
        disabled={!!loading}
        onClick={() => onVote('like')}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 5,
          border: 'none',
          fontSize: 13,
          borderRadius: 16,
          padding: '3px 14px',
          cursor: loading ? 'default' : 'pointer',
          lineHeight: '22px',
          fontWeight: isLikeSelected ? 600 : 400,
          color: isLikeSelected ? '#fff' : '#94a3b8',
          background: isLikeLoading ? '#bbf7d0' : isLikeSelected ? '#22c55e' : '#f1f5f9',
          boxShadow: isLikeSelected ? '0 1px 4px rgba(34,197,94,0.35)' : 'none',
          transition: 'all 0.25s ease',
          opacity: loading && !isLikeLoading ? 0.5 : 1,
        }}
        onMouseEnter={(e) => {
          if (!loading && !isLikeSelected) {
            e.currentTarget.style.background = '#dcfce7';
            e.currentTarget.style.color = '#16a34a';
          }
        }}
        onMouseLeave={(e) => {
          if (!loading && !isLikeSelected) {
            e.currentTarget.style.background = '#f1f5f9';
            e.currentTarget.style.color = '#94a3b8';
          }
        }}
      >
        {isLikeLoading ? (
          <LoadingOutlined style={{ fontSize: 14, color: '#16a34a' }} />
        ) : isLikeSelected ? (
          <LikeFilled style={{ fontSize: 14 }} />
        ) : (
          <LikeOutlined style={{ fontSize: 14 }} />
        )}
        {isLikeLoading ? t('knowledgeSearch.submitting') : t('knowledgeSearch.helpful')}
      </button>

      {/* 无帮助：点踩原因 + 备注走 Popconfirm（按钮下方弹出） */}
      <Popconfirm
        open={dislikeOpen}
        onOpenChange={onDislikeOpenChange}
        placement="bottom"
        icon={null}
        title={t('knowledgeSearch.feedbackReasonTitle')}
        description={
          <div style={{ width: 300 }}>
            <div style={{ marginBottom: 8, fontSize: 13, fontWeight: 600, color: '#334155' }}>
              {t('knowledgeSearch.feedbackReasonLabel')}
            </div>
            <Radio.Group
              value={voteReason}
              onChange={(e) => onVoteReasonChange(e.target.value)}
              style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8 }}
            >
              {FEEDBACK_REASONS.map((reason) => (
                <Radio key={reason} value={reason} style={{ fontSize: 13, color: '#475569' }}>
                  {t(`knowledgeSearch.feedbackReason_${reason}`)}
                </Radio>
              ))}
            </Radio.Group>
            <div style={{ marginTop: 14, marginBottom: 6, fontSize: 13, fontWeight: 600, color: '#334155' }}>
              {t('knowledgeSearch.feedbackCommentLabel')}
            </div>
            <Input.TextArea
              value={voteComment}
              onChange={(e) => onVoteCommentChange(e.target.value)}
              maxLength={300}
              rows={3}
              placeholder={t('knowledgeSearch.feedbackCommentPlaceholder')}
            />
          </div>
        }
        okText={t('knowledgeSearch.feedbackSubmit')}
        cancelText={t('common.cancel')}
        okButtonProps={{ loading: isDislikeLoading }}
        onConfirm={onDislikeConfirm}
      >
        <button
          type="button"
          disabled={!!loading}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 5,
            border: 'none',
            fontSize: 13,
            borderRadius: 16,
            padding: '3px 14px',
            cursor: loading ? 'default' : 'pointer',
            lineHeight: '22px',
            fontWeight: isDislikeSelected ? 600 : 400,
            color: isDislikeSelected ? '#fff' : '#94a3b8',
            background: isDislikeLoading ? '#fecaca' : isDislikeSelected ? '#ef4444' : '#f1f5f9',
            boxShadow: isDislikeSelected ? '0 1px 4px rgba(239,68,68,0.35)' : 'none',
            transition: 'all 0.25s ease',
            opacity: loading && !isDislikeLoading ? 0.5 : 1,
          }}
          onMouseEnter={(e) => {
            if (!loading && !isDislikeSelected) {
              e.currentTarget.style.background = '#ffe4e6';
              e.currentTarget.style.color = '#e11d48';
            }
          }}
          onMouseLeave={(e) => {
            if (!loading && !isDislikeSelected) {
              e.currentTarget.style.background = '#f1f5f9';
              e.currentTarget.style.color = '#94a3b8';
            }
          }}
        >
          {isDislikeLoading ? (
            <LoadingOutlined style={{ fontSize: 14, color: '#e11d48' }} />
          ) : isDislikeSelected ? (
            <DislikeFilled style={{ fontSize: 14 }} />
          ) : (
            <DislikeOutlined style={{ fontSize: 14 }} />
          )}
          {isDislikeLoading ? t('knowledgeSearch.submitting') : t('knowledgeSearch.unhelpful')}
        </button>
      </Popconfirm>
    </div>
  );
}

// ── 解析 <think> 标签 ──
function parseThink(raw: string): { think: string; answer: string; thinking: boolean } {
  const start = raw.indexOf('<think>');
  if (start < 0) return { think: '', answer: raw.trim(), thinking: false };
  const before = raw.slice(0, start);
  const afterStart = raw.slice(start + '<think>'.length);
  const end = afterStart.indexOf('</think>');
  if (end < 0) return { think: afterStart.trim(), answer: before.trim(), thinking: true };
  const think = afterStart.slice(0, end).trim();
  const answer = `${before}${afterStart.slice(end + '</think>'.length)}`.trim();
  return { think, answer, thinking: false };
}

// ── 解析引用 ──
function parseReferences(text: string): { refs: string[]; body: string } {
  const refs: string[] = [];
  const normalized = text.replace(/\r\n/g, '\n');
  const sourcePathPattern = /(?:\/app\/inputs\/|\/app\/outputs\/|\/tmp\/rag_output\/)/;
  const normalizeReferenceLine = (line: string) =>
    line
      .replace(/^\s*(?:#{1,6}\s*)?(?:References|参考文献|原文引用|引用来源)\s*[:：]?\s*/i, '')
      .replace(/^\s*[-*+]\s*/, '')
      .replace(/^\s*\d+[.)]\s*/, '')
      .replace(/^\s*\[\d+\]\s*/, '')
      .trim();
  const collectRef = (line: string) => {
    const cleaned = normalizeReferenceLine(line);
    if (cleaned) refs.push(cleaned);
  };
  const isReferenceHeading = (line: string) =>
    /^\s{0,3}(?:#{1,6}\s*)?(?:References|参考文献|原文引用|引用来源)\s*[:：]?\s*/i.test(line);
  const hasReferencePayload = (line: string) => sourcePathPattern.test(normalizeReferenceLine(line));
  const isSourcePathLine = (line: string) => sourcePathPattern.test(normalizeReferenceLine(line));
  const bodyLines: string[] = [];
  let inReferenceBlock = false;
  normalized.split('\n').forEach((line) => {
    if (isReferenceHeading(line)) {
      inReferenceBlock = true;
      if (hasReferencePayload(line)) collectRef(line);
      return;
    }
    if (inReferenceBlock) {
      if (line.trim()) collectRef(line);
      return;
    }
    if (isSourcePathLine(line)) {
      collectRef(line);
      return;
    }
    bodyLines.push(line);
  });
  const body = bodyLines
    .join('\n')
    .replace(
      /^\s*(?:[-*+]\s*)?(?:\d+[.)]\s*)?(?:\[\d+\]\s*)?(?:\/app\/inputs\/|\/app\/outputs\/|\/tmp\/rag_output\/)[^\n]*/gm,
      (line) => {
        collectRef(line);
        return '';
      },
    )
    .replace(
      /^\s*(?:[-*+]\s*)?(?:\d+[.)]\s*)?(?:\[\d+\]\s*)?(?:References|参考文献|原文引用|引用来源)\s*[:：]\s*(?:\/app\/inputs\/|\/app\/outputs\/|\/tmp\/rag_output\/)[^\n]*/gim,
      (line) => {
        collectRef(line);
        return '';
      },
    )
    .trim();
  return { refs: Array.from(new Set(refs)), body };
}

function isSearchRunning(status?: string): boolean {
  return status === 'searching';
}

function getSearchStatusLabel(status: string, t: (key: string) => string): string {
  switch (status) {
    case 'searching':
      return t('status.searching');
    case 'done':
      return t('status.completed');
    case 'error':
      return t('status.searchFailed');
    case 'stopped':
      return t('status.stopped');
    case 'empty':
      return t('status.noResults');
    default:
      return t('status.pendingSearch');
  }
}

// ═══════════════════════════════════════════════════════════
// SessionCard 主组件
// ═══════════════════════════════════════════════════════════

interface SessionCardProps {
  session: SearchSession;
  domainName: string;
  thinkExpanded: boolean;
  reasoningExpanded: boolean;
  refsExpanded: boolean;
  sessionVote: SearchFeedbackType | null;
  feedbackLoading: SearchFeedbackType | null;
  thinkGenerating: boolean;
  onToggleThink: (sessionId: string) => void;
  onToggleReasoning: (sessionId: string) => void;
  onToggleRefs: (sessionId: string) => void;
  onStop: () => void;
  onReSearch: (query: string, domainId: string) => void;
  onClear: () => void;
  onVote: (type: SearchFeedbackType) => void;
  onOpenReference: (ref: KnowledgeReference, loc?: KnowledgeReferenceLocation) => void;
  /** 点踩 Popconfirm 受控状态与表单（由父级 KnowledgeSearch 持有） */
  dislikeOpen: boolean;
  voteReason: FeedbackReason | null;
  voteComment: string;
  onDislikeOpenChange: (open: boolean) => void;
  onVoteReasonChange: (reason: FeedbackReason) => void;
  onVoteCommentChange: (comment: string) => void;
  onDislikeConfirm: () => void;
}

export default function SessionCard({
  session,
  domainName,
  thinkExpanded,
  reasoningExpanded,
  refsExpanded,
  sessionVote,
  feedbackLoading,
  thinkGenerating,
  onToggleThink,
  onToggleReasoning,
  onToggleRefs,
  onStop,
  onReSearch,
  onClear,
  onVote,
  onOpenReference,
  dislikeOpen,
  voteReason,
  voteComment,
  onDislikeOpenChange,
  onVoteReasonChange,
  onVoteCommentChange,
  onDislikeConfirm,
}: SessionCardProps) {
  const { t } = useTranslation();

  const parsedAnswer = parseThink(session.rawAnswer);
  const { body: responseBody } = parseReferences(parsedAnswer.answer);
  const running = isSearchRunning(session.status);
  const hasThink = parsedAnswer.think.length > 0;
  const hasAnswerContent = responseBody.length > 0;
  const hasContent = hasThink || hasAnswerContent;
  const statusLabel = getSearchStatusLabel(session.status, t);

  // [jonex] §image-refs P3-2: 答案旁图片资产（与 prompt 同源的 references 中筛出）
  const imageAssets = React.useMemo(
    () => collectImageAssets(session.references),
    [session.references],
  );
  const openImageAsset = React.useCallback((asset: ImageAsset) => {
    window.open(asset.assetUrl, '_blank', 'noopener');
  }, []);

  const [entityDrawerOpen, setEntityDrawerOpen] = React.useState(false);
  const [drawerEntity, setDrawerEntity] = React.useState<OntologyInstanceRow | null>(null);

  const [ragRecallOpen, setRagRecallOpen] = React.useState(false);
  const [ragRecallItem, setRagRecallItem] = React.useState<RagRecallItem | null>(null);
  const [ragRecallRank, setRagRecallRank] = React.useState(1);
  const [ragRecallScore, setRagRecallScore] = React.useState<number | null>(null);
  const [chunkLoading, setChunkLoading] = React.useState(false);

  const openEntityDrawer = (hit: OntologyInstanceRow & { score?: number; kb_id?: string }) => {
    // 本体实体信息直接取自问答接口返回的 ontology_match hits（含 description/attributes），不再二次查询
    setDrawerEntity(hit);
    setEntityDrawerOpen(true);
  };

  const entityAttributeEntries = React.useMemo(() => {
    if (!drawerEntity?.attributes) return [];
    return Object.entries(drawerEntity.attributes).map(([key, value]) => ({ key, value }));
  }, [drawerEntity]);

  const openRagRecallDrawer = async (item: RagRecallItem, rank: number, score?: number | null) => {
    setRagRecallItem(item);
    setRagRecallRank(rank);
    setRagRecallScore(score ?? null);
    setRagRecallOpen(true);
    setChunkLoading(true);
    try {
      const detail = await getChunkDetail(item.doc_id, item.chunk_id);
      setRagRecallItem((prev) =>
        prev && prev.chunk_id === item.chunk_id ? { ...prev, text: detail.content || prev.text } : prev,
      );
    } catch (err: any) {
      message.warning(err?.message || t('knowledgeSearch.chunkLoadError'));
    } finally {
      setChunkLoading(false);
    }
  };

  return (
    <Card style={{ borderRadius: 12, marginBottom: 20 }} styles={{ body: { padding: '20px 24px' } }}>
      {/* Header */}
      <div
        style={{ marginBottom: 16, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}
      >
        <div
          style={{
            minWidth: 0,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 16,
            fontWeight: 600,
            color: '#0b2b5c',
          }}
        >
          <SearchOutlined style={{ color: '#3b82f6', flexShrink: 0 }} />
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{session.query}</span>
          <span
            style={{
              fontSize: 12,
              color: '#64748b',
              fontWeight: 400,
              background: '#f1f5f9',
              borderRadius: 999,
              padding: '2px 8px',
              flexShrink: 0,
            }}
          >
            {domainName}
          </span>
          {running && (
            <span style={{ fontSize: 12, color: '#94a3b8', fontWeight: 400, marginLeft: 8, flexShrink: 0 }}>
              <Spin size="small" style={{ marginRight: 4 }} />
              {t('knowledgeSearch.searching')}
            </span>
          )}
          {session.status === 'error' && (
            <span style={{ fontSize: 12, color: '#ef4444', fontWeight: 400, flexShrink: 0 }}>{statusLabel}</span>
          )}
          {session.status === 'stopped' && (
            <span style={{ fontSize: 12, color: '#f97316', fontWeight: 400, flexShrink: 0 }}>{statusLabel}</span>
          )}
          {session.status === 'done' && (
            <span style={{ fontSize: 12, color: '#10b981', fontWeight: 400, flexShrink: 0 }}>{statusLabel}</span>
          )}
          {session.source && (
            <span
              style={{
                fontSize: 11,
                color: '#8b5cf6',
                fontWeight: 400,
                background: '#f5f3ff',
                borderRadius: 999,
                padding: '2px 10px',
                flexShrink: 0,
                marginLeft: 'auto',
              }}
            >
              {t('knowledgeSearch.source', { source: session.source })}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          {running ? (
            <Button size="small" icon={<StopOutlined />} onClick={onStop}>
              {t('knowledgeSearch.stopGenerate')}
            </Button>
          ) : (
            <Button size="small" icon={<ReloadOutlined />} onClick={() => onReSearch(session.query, session.domainId)}>
              {t('knowledgeSearch.reSearch')}
            </Button>
          )}
          <Button size="small" icon={<CloseCircleOutlined />} onClick={onClear}>
            {t('knowledgeSearch.clear')}
          </Button>
        </div>
      </div>

      {/* Error */}
      {session.status === 'error' && (
        <div
          style={{
            padding: '12px 14px',
            background: '#fef2f2',
            border: '1px solid #fecaca',
            borderRadius: 10,
            color: '#dc2626',
            fontSize: 13,
            marginBottom: 16,
            lineHeight: 1.7,
          }}
        >
          {session.errorMessage || t('knowledgeSearch.searchFailed')}
        </div>
      )}

      {/* Thinking stream */}
      {hasThink && (
        <div
          style={{
            padding: '12px 14px',
            background: '#f8fafc',
            border: '1px solid #e2e8f0',
            borderRadius: 10,
            marginBottom: 12,
          }}
        >
          <button
            onClick={() => onToggleThink(session.id)}
            style={{
              width: '100%',
              padding: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              color: '#64748b',
              textAlign: 'left',
              height: 'auto',
              lineHeight: 'inherit',
              border: 'none',
              background: 'transparent',
              cursor: 'pointer',
              fontSize: 13,
            }}
          >
            {thinkExpanded ? <CaretDownOutlined /> : <CaretRightOutlined />}
            <span style={{ fontSize: 13, fontWeight: 600, color: '#475569' }}>
              {t('knowledgeSearch.thinkingProcess')}
            </span>
            {thinkGenerating ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12, color: '#94a3b8' }}>
                <Spin size="small" />
                {t('knowledgeSearch.generating')}
              </span>
            ) : (
              <span style={{ fontSize: 12, color: '#94a3b8' }}>
                {thinkExpanded ? t('knowledgeSearch.clickCollapse') : t('knowledgeSearch.collapsed')}
              </span>
            )}
          </button>
          {thinkExpanded ? (
            <div
              style={{
                marginTop: 10,
                padding: '10px 12px',
                background: '#ffffff',
                borderRadius: 8,
                fontSize: 13,
                color: '#64748b',
                lineHeight: 1.75,
                whiteSpace: 'pre-wrap',
              }}
            >
              {parsedAnswer.think}
            </div>
          ) : (
            <div
              style={{
                marginTop: 8,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontSize: 12,
                color: '#94a3b8',
              }}
            >
              {parsedAnswer.think.split('\n').find(Boolean) || t('knowledgeSearch.reasoningComplete')}
            </div>
          )}
        </div>
      )}

      {/* Reasoning chain */}
      {(() => {
        const reasoning = session.reasoning;
        return (
          (reasoning?.steps?.length ?? 0) > 0 && (
            <div
              style={{
                background: '#fbfaff',
                border: '1px solid #ece9fb',
                borderRadius: 10,
                padding: '12px 16px',
                marginBottom: 16,
              }}
            >
              <button
                onClick={() => onToggleReasoning(session.id)}
                style={{
                  width: '100%',
                  padding: 0,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  justifyContent: 'flex-start',
                  height: 'auto',
                  lineHeight: 'inherit',
                  border: 'none',
                  background: 'transparent',
                  cursor: 'pointer',
                  fontSize: 13,
                }}
              >
                {reasoningExpanded ? <CaretDownOutlined /> : <CaretRightOutlined />}
                <NodeIndexOutlined style={{ color: '#8b5cf6' }} />
                <span style={{ fontSize: 13, fontWeight: 600, color: '#6d28d9' }}>
                  {t('knowledgeSearch.reasoningProcess')}
                </span>
                <span style={{ fontSize: 12, color: '#a78bda' }}>
                  {t('knowledgeSearch.steps', {
                    count: reasoning!.steps.length,
                    source: reasoning!.final_source,
                  })}
                  {reasoning!.total_ms != null ? ` · ${reasoning!.total_ms}ms` : ''}
                </span>
              </button>
              {reasoningExpanded && (
                <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {reasoning!.steps.map((step: ReasoningStep, i: number) => {
                    const meta = reasoningStatusMeta(step.status, t);
                    return (
                      <div key={`${step.stage}-${i}`} style={{ display: 'flex', gap: 10 }}>
                        <div
                          style={{
                            width: 22,
                            height: 22,
                            borderRadius: '50%',
                            flexShrink: 0,
                            background: meta.bg,
                            color: meta.color,
                            fontSize: 11,
                            fontWeight: 700,
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                          }}
                        >
                          {i + 1}
                        </div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                            <span style={{ fontSize: 13, fontWeight: 600, color: '#334155' }}>{step.title}</span>
                            <span
                              style={{
                                fontSize: 11,
                                color: meta.color,
                                background: meta.bg,
                                borderRadius: 4,
                                padding: '0 6px',
                              }}
                            >
                              {meta.label}
                            </span>
                            {step.duration_ms != null && (
                              <span style={{ fontSize: 11, color: '#a3adbb' }}>{step.duration_ms}ms</span>
                            )}
                          </div>
                          {step.summary && (
                            <div style={{ fontSize: 12, color: '#64748b', marginTop: 2, lineHeight: 1.6 }}>
                              {step.summary}
                            </div>
                          )}
                          <StepDetail
                            step={step}
                            t={t}
                            onEntityClick={openEntityDrawer}
                            onRecallClick={openRagRecallDrawer}
                            references={session.references}
                            onOpenReference={onOpenReference}
                          />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
          )
        );
      })()}

      {/* Answer */}
      {(responseBody || running) && (
        <div
          style={{
            padding: '14px 16px',
            background: '#fafcff',
            border: '1px solid #e8edf5',
            borderRadius: 10,
            marginBottom: 16,
          }}
        >
          <div
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: '#6366f1',
              marginBottom: 10,
              display: 'flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <BulbOutlined />
            {t('knowledgeSearch.searchResult')}
            <span style={{ fontWeight: 400, fontSize: 12, color: '#94a3b8' }}>
              {t('knowledgeSearch.knowledgeAnalysis', { domain: domainName })}
            </span>
          </div>
          <div style={{ fontSize: 14, color: '#334155', lineHeight: 1.8 }}>
            <ReactMarkdown
              components={{
                h1: ({ children }) => (
                  <h1 style={{ fontSize: 20, lineHeight: 1.45, margin: '12px 0 8px', color: '#0f172a' }}>{children}</h1>
                ),
                h2: ({ children }) => (
                  <h2 style={{ fontSize: 18, lineHeight: 1.5, margin: '12px 0 8px', color: '#0f172a' }}>{children}</h2>
                ),
                h3: ({ children }) => (
                  <h3 style={{ fontSize: 16, lineHeight: 1.5, margin: '10px 0 6px', color: '#0f172a' }}>{children}</h3>
                ),
                p: ({ children }) => <p style={{ margin: '0 0 10px' }}>{children}</p>,
                ul: ({ children }) => <ul style={{ margin: '0 0 10px', paddingLeft: 20 }}>{children}</ul>,
                ol: ({ children }) => <ol style={{ margin: '0 0 10px', paddingLeft: 20 }}>{children}</ol>,
                li: ({ children }) => <li style={{ marginBottom: 4 }}>{children}</li>,
                strong: ({ children }) => <strong style={{ color: '#0f172a' }}>{children}</strong>,
              }}
            >
              {responseBody || (running ? t('knowledgeSearch.analyzing') : '')}
            </ReactMarkdown>
          </div>
          {running && hasContent && (
            <div style={{ marginTop: 8 }}>
              <Spin size="small" />
            </div>
          )}

          {session.status === 'done' && (
            <FeedbackButtons
              activeVote={sessionVote}
              loading={feedbackLoading}
              onVote={onVote}
              dislikeOpen={dislikeOpen}
              voteReason={voteReason}
              voteComment={voteComment}
              onDislikeOpenChange={onDislikeOpenChange}
              onVoteReasonChange={onVoteReasonChange}
              onVoteCommentChange={onVoteCommentChange}
              onDislikeConfirm={onDislikeConfirm}
            />
          )}
        </div>
      )}

      {/* [jonex] §image-refs P3-2: 答案相关图片条（answer 下方、References 上方） */}
      {imageAssets.length > 0 && (
        <div
          style={{
            background: '#fdfdff',
            border: '1px solid #e8edf5',
            borderRadius: 10,
            padding: '12px 16px',
            marginBottom: 16,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 13,
              fontWeight: 600,
              color: '#6366f1',
              marginBottom: 10,
            }}
          >
            <PictureOutlined />
            {t('knowledgeSearch.answerImages', { count: imageAssets.length })}
          </div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {imageAssets.map((a) => (
              <div
                key={`${a.docId}-${a.imageIdx}`}
                style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'center', maxWidth: 96 }}
              >
                <ImageAssetThumb asset={a} t={t} onOpen={openImageAsset} />
                {a.description && (
                  <div
                    style={{
                      fontSize: 11,
                      color: '#64748b',
                      textAlign: 'center',
                      lineHeight: 1.4,
                      maxWidth: 96,
                      overflow: 'hidden',
                    }}
                  >
                    {a.description.length > 48 ? `${a.description.slice(0, 48)}…` : a.description}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* References */}
      {(session.references?.length ?? 0) > 0 && (
        <div
          style={{
            background: '#f8fafc',
            borderLeft: '3px solid #3b82f6',
            borderRadius: '0 8px 8px 0',
            padding: '14px 18px',
            marginBottom: 16,
          }}
        >
          <button
            onClick={() => onToggleRefs(session.id)}
            style={{
              width: '100%',
              padding: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              justifyContent: 'flex-start',
              fontSize: 12,
              color: '#3b82f6',
              fontWeight: 600,
              height: 'auto',
              lineHeight: 'inherit',
              border: 'none',
              background: 'transparent',
              cursor: 'pointer',
            }}
          >
            {refsExpanded ? <CaretDownOutlined /> : <CaretRightOutlined />}
            <FileTextOutlined />
            {t('knowledgeSearch.relatedDocs', { count: session.references!.length })}
          </button>
          {refsExpanded && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 12 }}>
              {session.references!.map((ref: KnowledgeReference, i: number) => {
                const locs = ref.locations || [];
                const tsLoc = locs.find(
                  (l: KnowledgeReferenceLocation) => l.type === 'timestamp' && l.time_start != null,
                );
                const snippet = locs.find((l: KnowledgeReferenceLocation) => l.text)?.text || '';
                // [jonex] §image-refs P3-2: 本卡片内可直开的图片 location
                // （有 asset_url 才给 location 级打开，无则回落文档级）
                const imageLocs = locs.filter(
                  (l: KnowledgeReferenceLocation) => l.type === 'image' && l.asset_url && l.image_idx != null,
                );
                // md 文件命中片段：用 MarkdownContent 渲染保留格式，其余纯文本
                const isMarkdownRef = /\.(md|markdown)$/i.test(ref.file_name || '');
                return (
                  <div
                    key={`${ref.doc_id}-${i}`}
                    style={{ background: '#fff', border: '1px solid #e8edf5', borderRadius: 8, padding: '10px 12px' }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span
                        style={{
                          fontSize: 11,
                          color: '#64748b',
                          background: '#f1f5f9',
                          borderRadius: 4,
                          padding: '1px 7px',
                          flexShrink: 0,
                        }}
                      >
                        {getMediaLabel(ref.media_type, t)}
                      </span>
                      <span
                        style={{
                          fontSize: 13,
                          fontWeight: 600,
                          color: '#0b2b5c',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          maxWidth: 360,
                        }}
                        title={ref.file_name}
                      >
                        {ref.file_name}
                      </span>
                      {tsLoc && (
                        <span style={{ fontSize: 12, color: '#8b5cf6', flexShrink: 0 }}>
                          {formatTimestamp(tsLoc.time_start)}
                          {tsLoc.time_end != null ? ` - ${formatTimestamp(tsLoc.time_end)}` : ''}
                        </span>
                      )}
                      <a
                        className="yx-table-action"
                        style={{ marginLeft: 'auto', fontSize: 12, flexShrink: 0 }}
                        onClick={() => onOpenReference(ref, imageLocs[0] || tsLoc || locs[0])}
                      >
                        {ref.media_type === 'video'
                          ? tsLoc
                            ? t('knowledgeSearch.locatePlay')
                            : t('knowledgeSearch.viewVideo')
                          : ref.media_type === 'audio'
                            ? t('knowledgeSearch.playAudio')
                            : ref.media_type === 'image'
                              ? t('knowledgeSearch.viewImage')
                              : t('knowledgeSearch.viewOriginal')}
                      </a>
                    </div>
                    {/* [jonex] §image-refs P3-2: 卡片内图片缩略图（location 级打开原图） */}
                    {imageLocs.length > 0 && (
                      <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                        {imageLocs.map((l: KnowledgeReferenceLocation) => (
                          <ImageAssetThumb
                            key={`${ref.doc_id}-${l.image_idx}`}
                            compact
                            asset={{
                              docId: ref.doc_id,
                              imageIdx: l.image_idx!,
                              assetUrl: l.asset_url!,
                              pageNo: l.page_no,
                              description: (l.text || '').replace(/^图片描述：/, '').trim(),
                            }}
                            t={t}
                            onOpen={openImageAsset}
                          />
                        ))}
                      </div>
                    )}
                    {snippet &&
                      (isMarkdownRef ? (
                        <div
                          style={{
                            marginTop: 8,
                            background: '#fafcff',
                            borderRadius: 6,
                            padding: '8px 10px',
                            maxHeight: 240,
                            overflow: 'auto',
                          }}
                        >
                          <MarkdownContent content={snippet} />
                        </div>
                      ) : (
                        <div
                          style={{
                            marginTop: 8,
                            fontSize: 13,
                            color: '#475569',
                            lineHeight: 1.7,
                            background: '#fafcff',
                            borderRadius: 6,
                            padding: '8px 10px',
                            maxHeight: 120,
                            overflow: 'auto',
                            whiteSpace: 'pre-wrap',
                          }}
                        >
                          {snippet}
                        </div>
                      ))}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Searching spinner */}
      {running && !hasContent && (
        <div style={{ textAlign: 'center', padding: '30px 0' }}>
          <Spin size="default" />
          <div style={{ marginTop: 10, color: '#94a3b8', fontSize: 13 }}>{t('knowledgeSearch.searchingKnowledge')}</div>
        </div>
      )}

      <OntologyEntityDrawer
        open={entityDrawerOpen}
        loading={false}
        entity={drawerEntity}
        attributeEntries={entityAttributeEntries}
        onClose={() => setEntityDrawerOpen(false)}
      />

      <RagRecallDrawer
        open={ragRecallOpen}
        loading={chunkLoading}
        item={ragRecallItem}
        rank={ragRecallRank}
        score={ragRecallScore}
        onClose={() => setRagRecallOpen(false)}
      />
    </Card>
  );
}
