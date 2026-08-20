import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Empty, Input, Modal, Spin, message } from 'antd';
import { useStore } from '@/store';
import ReactMarkdown from 'react-markdown';
import {
  SearchOutlined,
  ArrowRightOutlined,
  BlockOutlined,
  NodeIndexOutlined,
  FileTextOutlined,
  BulbOutlined,
  DatabaseOutlined,
  StopOutlined,
  ReloadOutlined,
  CloseCircleOutlined,
  CaretDownOutlined,
  CaretRightOutlined,
  LikeOutlined,
  LikeFilled,
  DislikeOutlined,
  DislikeFilled,
  LoadingOutlined,
} from '@ant-design/icons';
import {
  getKnowledgeSearchOverview,
  getKnowledgeSearchDomains,
  getKnowledgeSearchHistory,
  saveKnowledgeSearchHistory,
  resolveKnowledgeReferences,
  deleteKnowledgeSearchHistory,
  clearKnowledgeSearchHistory,
  streamKnowledgeSearch,
  submitSearchFeedback,
  cancelSearchFeedback,
  DEFAULT_FAST_STRICT_CONFIG,
} from '@/api/knowledgeSearch';
import { useDocumentViewer } from '@/components/DocumentViewer';
import type {
  KnowledgeSearchOverview,
  KnowledgeSearchDomain,
  KnowledgeSearchStrictConfig,
  KnowledgeSearchHistoryItem,
  KnowledgeSearchRunStatus,
  KnowledgeReference,
  KnowledgeReferenceLocation,
  ReasoningTrace,
  ReasoningStep,
  SearchFeedbackType,
  SubmitSearchFeedbackParams,
} from '@/types/knowledgeSearch';
import SearchPanel from './SearchPanel';
import SearchHistorySidebar from './SearchHistorySidebar';
import SessionCard from './SessionCard';

const STAT_CARD_CONFIG: {
  key: keyof KnowledgeSearchOverview;
  labelKey: string;
  Icon: React.ComponentType<any>;
  bg: string;
  color: string;
}[] = [
  {
    key: 'totalDomains',
    labelKey: 'knowledgeSearch.knowledgeDomain',
    Icon: BlockOutlined,
    bg: '#eff6ff',
    color: '#3b82f6',
  },
  {
    key: 'totalEntities',
    labelKey: 'knowledgeSearch.knowledgeEntity',
    Icon: NodeIndexOutlined,
    bg: '#ecfdf5',
    color: '#10b981',
  },
  { key: 'sourceFiles', labelKey: 'common.sourceFiles', Icon: FileTextOutlined, bg: '#f5f3ff', color: '#8b5cf6' },
  { key: 'dataSources', labelKey: 'common.dataSources', Icon: DatabaseOutlined, bg: '#fff7ed', color: '#f97316' },
];

const EMPTY_OVERVIEW: KnowledgeSearchOverview = {
  totalDocuments: 0,
  totalEntities: 0,
  totalRelations: 0,
  todaySearches: 0,
  avgResponseTimeMs: 0,
  totalDomains: 0,
  sourceFiles: 0,
  dataSources: 0,
};

export interface SearchSession {
  id: string;
  query: string;
  domainId: string;
  rawAnswer: string;
  status: KnowledgeSearchRunStatus;
  errorMessage: string;
  source?: string;
  references?: KnowledgeReference[];
  reasoning?: ReasoningTrace | null;
  /** [jonex] 本次搜索实际使用的检索条件（再次搜索时复用） */
  strictConfig?: KnowledgeSearchStrictConfig | null;
}

function parseThink(raw: string): { think: string; answer: string; thinking: boolean } {
  const start = raw.indexOf('<think>');
  if (start < 0) return { think: '', answer: raw.trim(), thinking: false };

  const before = raw.slice(0, start);
  const afterStart = raw.slice(start + '<think>'.length);
  const end = afterStart.indexOf('</think>');

  if (end < 0) {
    return { think: afterStart.trim(), answer: before.trim(), thinking: true };
  }

  const think = afterStart.slice(0, end).trim();
  const answer = `${before}${afterStart.slice(end + '</think>'.length)}`.trim();
  return { think, answer, thinking: false };
}

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

function buildAnswerPreview(raw: string, maxLen = 100): string {
  const { answer } = parseThink(raw);
  const { body } = parseReferences(answer);
  const cleaned = body.replace(/\s+/g, ' ').trim();
  if (!cleaned) return '';
  return cleaned.length > maxLen ? cleaned.slice(0, maxLen) + '...' : cleaned;
}

function formatRelativeTime(isoStr: string, t: (key: string, opts?: any) => string): string {
  const now = Date.now();
  const then = new Date(isoStr).getTime();
  const diffMs = now - then;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return t('common.justNow');
  if (diffMin < 60) return t('common.minutesAgo', { count: diffMin });
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return t('common.hoursAgo', { count: diffHr });
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay === 1) return t('common.yesterday');
  if (diffDay < 7) return t('common.daysAgo', { count: diffDay });
  return isoStr.substring(0, 10);
}

function isSearchRunning(status?: KnowledgeSearchRunStatus): boolean {
  return status === 'searching';
}

// ── 反馈按钮组件（三态设计：idle / loading / selected） ─────────

function FeedbackButtons({
  activeVote,
  loading,
  onVote,
}: {
  activeVote: SearchFeedbackType | null;
  loading: SearchFeedbackType | null;
  onVote: (type: SearchFeedbackType) => void;
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
      <Button
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
      </Button>

      {/* 无帮助 */}
      <Button
        disabled={!!loading}
        onClick={() => onVote('dislike')}
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
      </Button>
    </div>
  );
}

const KnowledgeSearch = function KnowledgeSearch() {
  const { t } = useTranslation();
  const { global } = useStore();
  const [query, setQuery] = useState('');
  const [selectedDomain, setSelectedDomain] = useState('all');
  const [deepSearch, setDeepSearch] = useState(false);
  const [strictConfig, setStrictConfig] = useState<KnowledgeSearchStrictConfig>(DEFAULT_FAST_STRICT_CONFIG);

  const [overview, setOverview] = useState<KnowledgeSearchOverview | null>(null);
  const [domains, setDomains] = useState<KnowledgeSearchDomain[]>([]);
  const [history, setHistory] = useState<KnowledgeSearchHistoryItem[]>([]);
  // [jonex] 历史分页："查看更多"按页追加，loaded < total 时显示更多按钮
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);

  const [activeSearch, setActiveSearch] = useState<SearchSession | null>(null);
  const [pageLoading, setPageLoading] = useState(true);
  const [pageError, setPageError] = useState('');
  const [activeHistoryIndex, setActiveHistoryIndex] = useState<number | null>(null);
  const [thinkExpandedMap, setThinkExpandedMap] = useState<Record<string, boolean>>({});
  /** 按查询文本持久化反馈状态（相同问题再次搜索时恢复） */
  const voteCacheRef = useRef<Record<string, SearchFeedbackType>>({});
  /** 当前会话的反馈选中态 */
  const [sessionVote, setSessionVote] = useState<SearchFeedbackType | null>(null);
  /** 当前正在提交的反馈类型（加载态） */
  const [feedbackLoading, setFeedbackLoading] = useState<SearchFeedbackType | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const reasoningRef = useRef<Record<string, unknown> | null>(null);
  const isSearching = isSearchRunning(activeSearch?.status);

  // 按当前领域空间过滤服务候选（'all' 全领域项始终保留）
  // 当 currentSpaceId 为空时不过滤，展示所有可用服务
  const visibleDomains = useMemo(
    () => domains.filter((d) => d.id === 'all' || !global.currentSpaceId || d.space_id === global.currentSpaceId),
    [domains, global.currentSpaceId],
  );

  // 空间切换：若当前选中的服务不属于新空间，则回落到「全领域」
  useEffect(() => {
    setSelectedDomain((prev) => (prev === 'all' || visibleDomains.some((d) => d.id === prev) ? prev : 'all'));
  }, [visibleDomains]);

  // 原文段/视频预览：统一文档查看器
  const { openDocument, viewer } = useDocumentViewer();
  // 推理链面板展开态（按 session）
  const [reasoningExpandedMap, setReasoningExpandedMap] = useState<Record<string, boolean>>({});
  // 关联原文面板展开态（按 session，默认折叠）
  const [refsExpandedMap, setRefsExpandedMap] = useState<Record<string, boolean>>({});

  // 查看原文：全部走统一弹层；视频/音频带时间锚点时定位到时间点
  const openReference = useCallback(
    (ref: KnowledgeReference, loc?: KnowledgeReferenceLocation) => {
      // [jonex] §image-refs P3-2: image location 且有 asset_url → 直开原图；
      // 无 asset_url（local 后端/未上传）回落文档级打开
      if (loc?.type === 'image' && loc.asset_url) {
        window.open(loc.asset_url, '_blank', 'noopener');
        return;
      }
      openDocument({
        docId: ref.doc_id,
        fileName: ref.file_name,
        mediaType: ref.media_type,
        timeStart: loc?.time_start ?? null,
        timeEnd: loc?.time_end ?? null,
      });
    },
    [openDocument],
  );

  // ── initial data load ──────────────────────────────────
  useEffect(() => {
    let mounted = true;

    async function loadInitialData() {
      setPageLoading(true);
      try {
        const results = await Promise.allSettled([
          getKnowledgeSearchOverview(),
          getKnowledgeSearchDomains(global.currentSpaceId ?? undefined),
          getKnowledgeSearchHistory('', global.currentSpaceId ?? undefined),
        ] as const);

        if (!mounted) return;

        const failedLabels: string[] = [];
        const [overviewResult, domainResult, historyResult] = results;

        if (overviewResult.status === 'fulfilled') {
          setOverview(overviewResult.value);
        } else {
          failedLabels.push(t('knowledgeSearch.overviewLabel'));
          setOverview(EMPTY_OVERVIEW);
        }

        if (domainResult.status === 'fulfilled') {
          setDomains(domainResult.value);
        } else {
          failedLabels.push(t('knowledgeSearch.domainListLabel'));
          setDomains([]);
        }

        if (historyResult.status === 'fulfilled') {
          setHistory(historyResult.value.items);
          setHistoryTotal(historyResult.value.total);
        } else {
          failedLabels.push(t('knowledgeSearch.historyLabel'));
          setHistory([]);
          setHistoryTotal(0);
        }

        setPageError('');
        if (failedLabels.length) {
          message.warning({
            key: 'knowledge-search-initial-data-warning',
            content: t('knowledgeSearch.partialApiFailed', { labels: failedLabels.join('、') }),
          });
        }
        setPageLoading(false);
      } catch (error) {
        if (!mounted) return;
        setPageError(error instanceof Error ? error.message : t('knowledgeSearch.loadFailed'));
        setPageLoading(false);
      }
    }

    loadInitialData();

    return () => {
      mounted = false;
      abortRef.current?.abort();
    };
  }, [global.currentSpaceId]);

  // ── search ─────────────────────────────────────────────
  const getSelectedKbIds = useCallback(
    (domainId?: string): string[] => {
      if (!domainId || domainId === 'all') {
        const allIds = new Set<string>();
        visibleDomains.forEach((d) => d.kb_ids?.forEach((kid) => allIds.add(kid)));
        return Array.from(allIds);
      }
      const domain = visibleDomains.find((d) => d.id === domainId);
      return domain?.kb_ids ?? [];
    },
    [visibleDomains],
  );

  const handleSearch = useCallback(
    async (
      nextQuery?: string,
      options?: { keepHistoryActive?: boolean; domainId?: string; strictConfig?: KnowledgeSearchStrictConfig | null },
    ) => {
      const trimmedQuery = (nextQuery ?? query).trim();
      if (!trimmedQuery) return;

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      const sessionId = Date.now().toString();
      const domainId = options?.domainId ?? selectedDomain;
      // [jonex] 复用历史检索条件：优先用历史快照的 strictConfig，否则用当前面板配置
      const effectiveStrictConfig = options?.strictConfig ?? strictConfig;
      const kbIds = getSelectedKbIds(domainId);
      if (kbIds.length === 0) {
        message.warning(t('knowledgeSearch.noKbWarning'));
        return;
      }
      const searchParams = {
        query: trimmedQuery,
        mode: 'mix' as const,
        topK: 5,
        domainId,
        kbIds,
        deep: deepSearch,
        strictConfig: effectiveStrictConfig,
      };
      let streamError: Error | null = null;
      let accumulatedAnswer = '';
      let finalReferences: KnowledgeReference[] = [];
      const startTime = Date.now();

      setQuery(trimmedQuery);
      if (!options?.keepHistoryActive) setActiveHistoryIndex(null);
      setActiveSearch({
        id: sessionId,
        query: trimmedQuery,
        domainId,
        rawAnswer: '',
        status: 'searching',
        errorMessage: '',
        // [jonex] 检索条件快照：随本次会话保存，再次搜索时复用当时条件
        strictConfig: effectiveStrictConfig,
      });

      try {
        await streamKnowledgeSearch(
          searchParams,
          {
            onDelta: (delta, meta?: any) => {
              accumulatedAnswer += delta;
              setThinkExpandedMap((prev) => {
                if (!delta.includes('<think>')) return prev;
                return { ...prev, [sessionId]: true };
              });
              setActiveSearch((prev) =>
                prev?.id === sessionId
                  ? {
                      ...prev,
                      rawAnswer: prev.rawAnswer + delta,
                      source: meta?.source || prev.source,
                    }
                  : prev,
              );
            },
            onError: (error) => {
              streamError = error;
              setActiveSearch((prev) =>
                prev?.id === sessionId
                  ? {
                      ...prev,
                      status: 'error' as const,
                      errorMessage: error.message,
                    }
                  : prev,
              );
            },
            onDone: (meta) => {
              reasoningRef.current = (meta?.reasoning as any) ?? null;

              // 无答案检测：如果回答内容表示"无法回答"，清空引用
              const noAnswerPatterns = [
                /sorry/i,
                /unable to answer/i,
                /no answer/i,
                /cannot provide/i,
                /no relevant/i,
                /\[no-context\]/i,
                /无法回答/i,
                /无法提供/i,
                /未找到相关/i,
              ];
              const isNoAnswer = noAnswerPatterns.some((p) => p.test(accumulatedAnswer));
              const filteredRefs = isNoAnswer ? [] : (meta?.references ?? []);

              finalReferences = filteredRefs;
              setActiveSearch((prev) =>
                prev?.id === sessionId
                  ? {
                      ...prev,
                      references: filteredRefs,
                      reasoning: meta?.reasoning ?? null,
                      source: meta?.source || prev.source,
                    }
                  : prev,
              );
              // 恢复相同查询的投票状态
              const cached = voteCacheRef.current[trimmedQuery];
              if (cached) {
                setSessionVote(cached);
              } else {
                setSessionVote(null);
              }
            },
          },
          controller.signal,
        );

        if (streamError) throw streamError;
        if (controller.signal.aborted) return;

        const domainName = getDomainName(domainId);
        const preview = buildAnswerPreview(accumulatedAnswer);
        const refCount = finalReferences.length;

        setActiveSearch((prev) => (prev?.id === sessionId ? { ...prev, status: 'done' as const } : prev));
        setThinkExpandedMap((prev) => ({ ...prev, [sessionId]: false }));

        saveKnowledgeSearchHistory('', {
          domainSpaceId: global.currentSpaceId ?? undefined,
          query: trimmedQuery,
          domainId,
          domain: domainName,
          answerPreview: preview,
          referenceCount: refCount,
          resultCount: refCount,
          status: 'done',
          durationMs: Date.now() - startTime,
          // [jonex] 检索历史快照：完整答案 + 引用 + 推理链落库，点击历史直接展示（不重新检索）
          answer: accumulatedAnswer,
          references: finalReferences,
          reasoning: reasoningRef.current as ReasoningTrace | null,
          // [jonex] 检索条件快照：保存实际使用的 strictConfig，点击历史再次搜索时复用
          strictConfig: effectiveStrictConfig,
        })
          .then((item) => {
            if (!options?.keepHistoryActive) {
              setHistory((prev) => [item, ...prev]);
              setActiveHistoryIndex(0);
            }
          })
          .catch(() => {
            // save failed, silently ignore
          });
      } catch (error) {
        if (controller.signal.aborted) return;
        const msg = error instanceof Error ? error.message : t('knowledgeSearch.searchFailed');
        setActiveSearch((prev) =>
          prev?.id === sessionId ? { ...prev, status: 'error' as const, errorMessage: msg } : prev,
        );
        setThinkExpandedMap((prev) => ({ ...prev, [sessionId]: false }));
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [query, selectedDomain, getSelectedKbIds, deepSearch, strictConfig],
  );

  const handleHistoryClick = useCallback(
    (item: KnowledgeSearchHistoryItem, index: number) => {
      setQuery(item.query);
      setActiveHistoryIndex(index);
      if (item.domainId) setSelectedDomain(item.domainId);
      // [jonex] 历史快照：有 answer/references 时直接展示历史结果，不重新检索；
      // 旧数据无快照则回退为重新检索
      const hasSnapshot = Boolean(item.answer) || (item.references?.length ?? 0) > 0;
      if (hasSnapshot) {
        abortRef.current?.abort();
        abortRef.current = null;
        setSessionVote(null);
        const sessionId = `history-${item.id}`;
        setActiveSearch({
          id: sessionId,
          query: item.query,
          domainId: item.domainId ?? selectedDomain,
          rawAnswer: item.answer ?? '',
          status: 'done',
          errorMessage: '',
          references: item.references ?? [],
          reasoning: item.reasoning ?? null,
          // [jonex] 检索条件快照：点击历史再次搜索时复用当时的 strictConfig
          strictConfig: item.strictConfig ?? null,
        });
        // 快照里的 raw_url 已剥离且会过期，按 doc_id/locations 重新富化
        if (item.references?.length) {
          resolveKnowledgeReferences(item.references)
            .then((resolved) => {
              setActiveSearch((prev) =>
                prev?.id === sessionId ? { ...prev, references: resolved } : prev,
              );
            })
            .catch(() => {
              // resolve 失败时保留快照引用（doc_id 仍可打开原文，仅 raw_url 为空）
            });
        }
        return;
      }
      void handleSearch(item.query, {
        keepHistoryActive: true,
        domainId: item.domainId ?? selectedDomain,
        strictConfig: item.strictConfig ?? undefined,
      });
    },
    [handleSearch, selectedDomain],
  );

  const handleStopSearch = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setActiveSearch((prev) => {
      if (!prev || !isSearchRunning(prev.status)) return prev;
      setThinkExpandedMap((map) => ({ ...map, [prev.id]: false }));
      return { ...prev, status: 'stopped' as const };
    });
    message.info(t('knowledgeSearch.searchStopped'));
  }, []);

  const handleClearSearch = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setActiveSearch(null);
    setActiveHistoryIndex(null);
  }, []);

  /** 对当前回答点击「有帮助/无帮助」—— 加载态 + 匹配后端真实响应格式 */
  const handleVoteAnswer = useCallback(
    async (feedbackType: SearchFeedbackType) => {
      if (!activeSearch || feedbackLoading) return;
      const sessionId = activeSearch.id;

      // 从引用中提取所有不重复的知识库 ID
      const kbIds: string[] = [];
      if (activeSearch.references) {
        const seen = new Set<string>();
        activeSearch.references.forEach((ref) => {
          if (ref.kb_id && !seen.has(ref.kb_id)) {
            seen.add(ref.kb_id);
            kbIds.push(ref.kb_id);
          }
        });
      }
      if (kbIds.length === 0) {
        message.warning(t('knowledgeSearch.noRefFeedback'));
        return;
      }

      setFeedbackLoading(feedbackType);

      try {
        // 情况1：点击同一个按钮 → 取消反馈
        if (sessionVote === feedbackType) {
          await cancelSearchFeedback({ sessionId, feedbackType, kbIds });
          setSessionVote(null);
          if (activeSearch) delete voteCacheRef.current[activeSearch.query];
        }
        // 情况2：点击不同按钮或第一次 → 提交新反馈
        else {
          const answerText = parseThink(activeSearch.rawAnswer).answer;
          const { body: cleanBody } = parseReferences(answerText);
          const preview = cleanBody.replace(/\s+/g, ' ').trim().slice(0, 100);

          // 如果之前选了其他类型，先取消旧的
          if (sessionVote) {
            await cancelSearchFeedback({ sessionId, feedbackType: sessionVote, kbIds }).catch(() => {});
          }

          await submitSearchFeedback({
            sessionId,
            query: activeSearch.query,
            answerPreview: preview,
            feedbackType,
            kbIds,
            searchedAt: new Date().toISOString(),
          });

          setSessionVote(feedbackType);
          if (activeSearch) voteCacheRef.current[activeSearch.query] = feedbackType;
        }
      } catch {
        message.error(t('knowledgeSearch.operationFailed'));
      } finally {
        setFeedbackLoading(null);
      }
    },
    [activeSearch, sessionVote, feedbackLoading],
  );

  const handleDomainChange = useCallback((value: string) => {
    setSelectedDomain(value);
  }, []);

  const handleDeleteHistory = useCallback(
    (id: string, index: number) => {
      deleteKnowledgeSearchHistory('', id)
        .then(() => {
          setHistory((prev) => prev.filter((h) => h.id !== id));
          if (activeHistoryIndex === index) setActiveHistoryIndex(null);
        })
        .catch(() => message.error(t('knowledgeSearch.operationFailed')));
    },
    [activeHistoryIndex, t],
  );

  const handleClearHistory = useCallback(() => {
    Modal.confirm({
      title: t('knowledgeSearch.clearSearchHistory'),
      content: t('knowledgeSearch.clearSearchHistoryConfirm'),
      okText: t('knowledgeSearch.confirmClear'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: () => {
        clearKnowledgeSearchHistory('')
          .then(() => {
            setHistory([]);
            setHistoryTotal(0);
            setHistoryPage(1);
            setActiveHistoryIndex(null);
          })
          .catch(() => message.error(t('knowledgeSearch.operationFailed')));
      },
    });
  }, [t]);

  // [jonex] 历史"查看更多"：按页追加，去重后置底；已加载条数达总数后隐藏按钮
  const loadMoreHistory = useCallback(async () => {
    if (historyLoadingMore) return;
    const nextPage = historyPage + 1;
    setHistoryLoadingMore(true);
    try {
      const result = await getKnowledgeSearchHistory('', global.currentSpaceId ?? undefined, nextPage, 20);
      setHistory((prev) => {
        const existing = new Set(prev.map((h) => h.id));
        const fresh = (result.items ?? []).filter((h) => !existing.has(h.id));
        return [...prev, ...fresh];
      });
      setHistoryTotal(result.total);
      setHistoryPage(nextPage);
    } catch {
      message.warning(t('knowledgeSearch.operationFailed'));
    } finally {
      setHistoryLoadingMore(false);
    }
  }, [historyLoadingMore, historyPage, global.currentSpaceId, t]);

  const getDomainName = useCallback(
    (domainId?: string) => domains.find((d) => d.id === domainId)?.name ?? t('knowledgeSearch.allDomain'),
    [domains],
  );

  // ── render helpers ─────────────────────────────────────

  const renderStats = () => (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16, marginBottom: 28 }}>
      {STAT_CARD_CONFIG.map((cfg) => {
        const value = overview ? overview[cfg.key] : '-';
        const displayValue = typeof value === 'number' ? value.toLocaleString() : value;
        return (
          <Card
            key={cfg.key}
            style={{ borderRadius: 12 }}
            styles={{
              body: { padding: '18px 20px', display: 'flex', alignItems: 'center', gap: 14 },
            }}
          >
            <div
              style={{
                width: 48,
                height: 48,
                borderRadius: 12,
                background: cfg.bg,
                color: cfg.color,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 22,
                flexShrink: 0,
              }}
            >
              <cfg.Icon />
            </div>
            <div>
              <div style={{ fontSize: 22, fontWeight: 700, color: '#0b2b5c', lineHeight: 1.2 }}>
                {pageLoading ? '-' : displayValue}
              </div>
              <div style={{ fontSize: 13, color: '#94a3b8', marginTop: 2 }}>{t(cfg.labelKey)}</div>
            </div>
          </Card>
        );
      })}
    </div>
  );

  const renderMainContent = () => {
    if (pageLoading) {
      return (
        <div style={{ textAlign: 'center', padding: '80px 0' }}>
          <Spin size="large" />
        </div>
      );
    }

    if (pageError) {
      return (
        <div style={{ textAlign: 'center', padding: '80px 0' }}>
          <div style={{ color: '#ef4444', fontSize: 14, marginBottom: 12 }}>{pageError}</div>
          <Button onClick={() => window.location.reload()}>{t('knowledgeSearch.refreshPage')}</Button>
        </div>
      );
    }

    if (!activeSearch) {
      return (
        <Card style={{ borderRadius: 12 }} styles={{ body: { padding: '60px 20px' } }}>
          <Empty description={t('knowledgeSearch.emptyDescription')} />
        </Card>
      );
    }

    const thinkParsed = parseThink(activeSearch.rawAnswer);
    return (
      <SessionCard
        session={activeSearch}
        domainName={getDomainName(activeSearch.domainId)}
        thinkExpanded={thinkParsed.thinking || thinkExpandedMap[activeSearch.id] === true}
        reasoningExpanded={reasoningExpandedMap[activeSearch.id] === true}
        refsExpanded={refsExpandedMap[activeSearch.id] === true}
        thinkGenerating={thinkParsed.thinking}
        sessionVote={sessionVote}
        feedbackLoading={feedbackLoading}
        onToggleThink={(id) => setThinkExpandedMap((prev) => ({ ...prev, [id]: !prev[id] }))}
        onToggleReasoning={(id) => setReasoningExpandedMap((prev) => ({ ...prev, [id]: !prev[id] }))}
        onToggleRefs={(id) => setRefsExpandedMap((prev) => ({ ...prev, [id]: !prev[id] }))}
        onStop={handleStopSearch}
        onReSearch={(query, domainId) =>
          void handleSearch(query, { domainId, strictConfig: activeSearch?.strictConfig ?? undefined })
        }
        onClear={handleClearSearch}
        onVote={handleVoteAnswer}
        onOpenReference={openReference}
      />
    );
  };

  // ── main render ────────────────────────────────────────
  return (
    <div>
      <div className="yx-page-title">
        <h1>{t('knowledgeSearch.pageTitle')}</h1>
        <p className="yx-page-subtitle">
          <span
            dangerouslySetInnerHTML={{
              __html: t('knowledgeSearch.pageSubtitle', {
                name: global.currentSpace?.name || t('knowledgeSearch.notSelected'),
              }),
            }}
          />
        </p>
      </div>
      {/* 统计信息-暂时隐藏 */}
      {/* {renderStats()} */}
      <SearchPanel
        selectedDomain={selectedDomain}
        visibleDomains={visibleDomains}
        query={query}
        isSearching={isSearching}
        deepSearch={deepSearch}
        onDeepSearchChange={setDeepSearch}
        strictConfig={strictConfig}
        onStrictConfigChange={setStrictConfig}
        onDomainChange={handleDomainChange}
        onQueryChange={setQuery}
        onSearch={() => void handleSearch()}
        onStop={handleStopSearch}
      />

      <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>
        <div style={{ flex: 1, minWidth: 0 }}>{renderMainContent()}</div>
        <SearchHistorySidebar
          history={history}
          activeHistoryIndex={activeHistoryIndex}
          onHistoryClick={handleHistoryClick}
          onDeleteHistory={handleDeleteHistory}
          onClearHistory={handleClearHistory}
          hasMore={history.length < historyTotal}
          onLoadMore={() => void loadMoreHistory()}
          loadingMore={historyLoadingMore}
        />
      </div>

      <div style={{ textAlign: 'center', padding: '32px 0 8px', fontSize: 13, color: '#cbd5e1' }}>
        {t('knowledgeSearch.footer')}
      </div>

      {/* 统一文档查看弹层（视频/音频可定位到时间点） */}
      {viewer}
    </div>
  );
};

export default KnowledgeSearch;
