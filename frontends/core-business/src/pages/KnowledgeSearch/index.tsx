import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Modal, Spin, message } from 'antd';
import { useStore } from '@/store';
import {
  BlockOutlined,
  NodeIndexOutlined,
  FileTextOutlined,
  DatabaseOutlined,
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
  getKnowledgeSearchHistorySnapshot,
  saveKnowledgeSearchHistory,
  resolveKnowledgeReferences,
  deleteKnowledgeSearchHistory,
  clearKnowledgeSearchHistory,
  streamKnowledgeSearch,
  submitAnswerFeedback as submitAnswerFeedbackApi,
  getAnswerFeedback,
  recognizeImageQuery,
  DEFAULT_FAST_STRICT_CONFIG,
} from '@/api/knowledgeSearch';
import {
  compressImage,
  formatBytes,
  ImageDecodeError,
  isAcceptedImage,
} from '@/utils/imageCompress';
import { useDocumentViewer } from '@/components/DocumentViewer';
import { toSpanRequest } from '@/components/documentBBox';
import type {
  AttachedImage,
  ImageQueryLimits,
  ImageQueryResult,
  KnowledgeSearchOverview,
  KnowledgeSearchDomain,
  KnowledgeSearchStrictConfig,
  KnowledgeSearchUiMode,
  KnowledgeSearchHistoryItem,
  KnowledgeSearchRunStatus,
  KnowledgeReference,
  KnowledgeReferenceLocation,
  WikiProgressEvent,
  ReasoningTrace,
  ReasoningStep,
  SearchFeedbackType,
  FeedbackReason,
} from '@/types/knowledgeSearch';
import { PageContent } from '@jonex/shared-lib';
import SearchPanel from './SearchPanel';
import SearchHistorySidebar from './SearchHistorySidebar';
import SessionCard from './SessionCard';
import WelcomeEmpty from './WelcomeEmpty';
import WikiEvidenceDrawer from './WikiEvidenceDrawer';
import styles from './PageLayout.module.scss';

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
  wiki?: boolean;
  wikiProgress?: WikiProgressEvent[];
  /** 本次检索使用的档位（块头标签用；历史回放可为空） */
  mode?: KnowledgeSearchUiMode;
  startedAt?: number;
  elapsedMs?: number;
  rawAnswer: string;
  status: KnowledgeSearchRunStatus;
  errorMessage: string;
  source?: string;
  references?: KnowledgeReference[];
  reasoning?: ReasoningTrace | null;
  /** [jonex] 本次搜索实际使用的检索条件（再次搜索时复用） */
  strictConfig?: KnowledgeSearchStrictConfig | null;
  /** [jonex] answer-feedback 锚点：检索历史记录 id（检索响应下发 / 历史项 id），无则无法记录反馈 */
  historyId?: string;
  // ── [jonex] 以图问知识 ──
  /**
   * 本轮附带的图片。两个来源：
   * - 当次检索：`localUrl`（本地 objectURL，用来即时预览原始附图）；
   * - 历史回放：`viewUrl`（服务端短时票据）。
   * 识别失败的张 `status='failed'`，卡片上标「未参与检索」。
   */
  queryImages?: SessionQueryImage[];
  /** 用户原话（点检索时输入框里的文字）；历史回放时来自 `metadata.image_question` */
  imageQuestion?: string;
  /** 识别回包（会话卡片上「识别为：…」与每张图的描述 / OCR 都取自它） */
  recognized?: ImageQueryResult;
  /**
   * 「重试」用的原图。只保留压缩后的 Blob —— **原图不出浏览器**（D4）。
   * 识别成功或卡片被移除后即可丢弃。
   */
  retryFiles?: Blob[];
}

export interface SessionQueryImage {
  imageId?: string | null;
  /** 本地预览（当次检索） */
  localUrl?: string;
  /** 服务端查看地址（历史回放） */
  viewUrl?: string | null;
  status?: 'ok' | 'failed';
  errorCode?: number | null;
}

/** 内置默认上限（与后端默认值一致）；收到识别回包后改用回包里的 `limits` */
const DEFAULT_IMAGE_LIMITS: ImageQueryLimits = {
  maxImages: 4,
  maxBytes: 5 * 1024 * 1024,
  maxTotalBytes: 12 * 1024 * 1024,
};

/** 生成 operation_id（优先 crypto.randomUUID，兼容非安全上下文） */
function genUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
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
  // [jonex] 以图问知识：识别阶段也算「跑着」—— 用户此时看到的是「识别图片中…」，
  // 「停止」按钮该在，且不该再让他点一次检索。
  return status === 'searching' || status === 'recognizing';
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
  const [selectedDomain, setSelectedDomain] = useState('');
  // [jonex] 检索模式（按钮组）：fast=快速检索 /search/mix，deep=深度检索 /search/deep，
  // wiki=Wiki 检索 /search/jonex_wiki（只落在 jonex_wiki 链路的库上）
  const [searchMode, setSearchMode] = useState<KnowledgeSearchUiMode>('fast');
  const [wikiThinkingMode, setWikiThinkingMode] = useState<'fast' | 'standard' | 'deep'>('fast');
  const [strictConfig, setStrictConfig] = useState<KnowledgeSearchStrictConfig>(DEFAULT_FAST_STRICT_CONFIG);

  const [overview, setOverview] = useState<KnowledgeSearchOverview | null>(null);
  const [domains, setDomains] = useState<KnowledgeSearchDomain[]>([]);
  const [history, setHistory] = useState<KnowledgeSearchHistoryItem[]>([]);
  // [jonex] 历史分页："查看更多"按页追加，loaded < total 时显示更多按钮
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  /**
   * [jonex] 正在按 id 拉快照的那条历史（列表不再携带快照，点击时才取）。
   * 侧栏据此在那一条上转圈，而不是让整页等。
   */
  const [historySnapshotLoadingId, setHistorySnapshotLoadingId] = useState<string | null>(null);
  /** 点击序号：连续点击时丢弃先发后到的旧响应，避免 A 的结果盖掉 B 的会话 */
  const historyClickSeqRef = useRef(0);

  const [activeSearch, setActiveSearch] = useState<SearchSession | null>(null);
  /** 同步 activeSearch 的 ref（异步回显 / 提交后判断会话是否仍为当前） */
  const activeSearchRef = useRef<SearchSession | null>(null);
  useEffect(() => {
    activeSearchRef.current = activeSearch;
  }, [activeSearch]);

  /**
   * 当前空间 id 的**实时值**。
   *
   * 请求发出去之后才回来的回调，闭包里读到的 `global.currentSpaceId` 是发起那一刻的旧值，
   * 拿它判断不了「等到回包时空间有没有换过」——那正是「旧空间的数据漏进新空间」的来源。
   * 判断必须对着 ref。
   */
  const currentSpaceIdRef = useRef(global.currentSpaceId);
  useEffect(() => {
    currentSpaceIdRef.current = global.currentSpaceId;
  }, [global.currentSpaceId]);
  const [pageLoading, setPageLoading] = useState(true);
  const [pageError, setPageError] = useState('');
  const [activeHistoryIndex, setActiveHistoryIndex] = useState<number | null>(null);
  const [thinkExpandedMap, setThinkExpandedMap] = useState<Record<string, boolean>>({});
  /** 按 history_id 持久化反馈状态（打开同一检索历史时恢复） */
  const voteCacheRef = useRef<Record<string, SearchFeedbackType>>({});
  /** 按 history_id 记录已成功提交的版本号（下次提交 version+1，与后端单调递增对齐） */
  const versionCacheRef = useRef<Record<string, number>>({});
  /** 进行中的提交 operation_id（失败重试复用，实现幂等） */
  const operationIdRef = useRef<{ historyId: string; operationId: string } | null>(null);
  /** 按 history_id 回显的点踩原因/备注（点踩弹框预填，供查看/修改） */
  const echoedFeedbackRef = useRef<Record<string, { reason: FeedbackReason | null; comment: string }>>({});
  /** 当前会话的反馈选中态 */
  const [sessionVote, setSessionVote] = useState<SearchFeedbackType | null>(null);
  /** 当前正在提交的反馈类型（加载态） */
  const [feedbackLoading, setFeedbackLoading] = useState<SearchFeedbackType | null>(null);
  /** 点踩原因弹框 */
  const [dislikeOpen, setDislikeOpen] = useState(false);
  const [dislikeHistoryId, setDislikeHistoryId] = useState('');
  const [voteReason, setVoteReason] = useState<FeedbackReason | null>(null);
  const [voteComment, setVoteComment] = useState('');

  const abortRef = useRef<AbortController | null>(null);
  const reasoningRef = useRef<Record<string, unknown> | null>(null);
  const isSearching = isSearchRunning(activeSearch?.status);

  // ── [jonex] 以图问知识：输入区待检索的图片 ──
  // 选图 / 粘贴时**逐张压缩后追加**，只做本地预览，**不发任何请求**（D2）——
  // 正式识别要等用户点「检索」。
  const [attachedImages, setAttachedImages] = useState<AttachedImage[]>([]);
  // 上限以后端回包为准（D7）：先内置默认值，收到 `limits` 后更新。
  const [imageLimits, setImageLimits] = useState<ImageQueryLimits>(DEFAULT_IMAGE_LIMITS);
  // 卸载时 revoke 用：objectURL 不回收会一直占着内存里的图片
  const attachedImagesRef = useRef<AttachedImage[]>([]);
  useEffect(() => {
    attachedImagesRef.current = attachedImages;
  }, [attachedImages]);

  /**
   * 正在压缩、还没进 state 的张数。
   *
   * 名额判定要把它算进去：压缩是异步的，连续选两批时两次调用会读到同一个旧的
   * `attachedImages.length`，各自以为还有名额 → 合计超上限。
   */
  const attachInFlightRef = useRef(0);

  /**
   * 输入区的「换代」号：切空间 / 切检索维度时 +1。
   *
   * 压缩是异步的，「选完图那一瞬间切了空间」会留下一批没有归属的图 —— 它们是在上一个
   * 上下文里选中的，重置之后再追加回来就会凭空出现在默认态里。发起压缩时记下代号，
   * 追加进 state 前对不上就整批丢弃。
   */
  const attachEpochRef = useRef(0);

  /**
   * 当前**会话**用到的 objectURL（输入区那批在移入会话时所有权也转移过来）。
   *
   * ⚠️ 这些 URL 原来没人回收：会话卡片与重试各建各的，换一次检索就漏一批，
   * 图片字节一直留在内存里。归口到这里，会话被替换 / 清空 / 组件卸载时统一 revoke。
   * 服务端的 `viewUrl` 是普通 http 地址，不是 objectURL，不在其中。
   */
  const sessionUrlsRef = useRef<string[]>([]);
  const releaseSessionUrls = useCallback(() => {
    sessionUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    sessionUrlsRef.current = [];
  }, []);

  useEffect(
    () => () => {
      // 卸载：输入区与会话两处的 URL 都要回收
      attachedImagesRef.current.forEach((image) => URL.revokeObjectURL(image.objectUrl));
      attachedImagesRef.current = [];
      sessionUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
      sessionUrlsRef.current = [];
    },
    [],
  );

  /**
   * 追加图片：逐张压缩 → 超上限的部分丢弃并提示。
   *
   * 压缩在**这里**做（而不是点检索时）：选完图立刻能看到压缩后的样子，
   * 且点「检索」那一刻只剩上传，交互更跟手。
   */
  const handleAttachImages = useCallback(
    async (files: File[] | Blob[]) => {
      // ⚠️ 选图框的 `accept` 只管「点按钮选文件」那一条路；**粘贴与拖拽绕过它**，
      // 所以类型白名单统一在这里过一遍，并明确告知（静默丢弃会让用户以为图附上了）。
      const all = Array.from(files ?? []);
      const incoming = all.filter(isAcceptedImage);
      if (incoming.length < all.length) {
        message.warning(t('knowledgeSearch.imageUnsupported'));
      }
      if (!incoming.length) return;

      // ⚠️ 名额要算上**"正在压缩、还没进 state"**的那一批：压缩是异步的，
      // 连续选两批时两次调用都会读到同一个旧的 `attachedImages.length`，
      // 各自认为还有名额 → 合计超上限。`attachInFlightRef` 记的就是这个窗口。
      const room = imageLimits.maxImages - attachedImages.length - attachInFlightRef.current;
      if (room <= 0) {
        message.warning(t('knowledgeSearch.imageLimitReached', { count: imageLimits.maxImages }));
        return;
      }

      const accepted: AttachedImage[] = [];
      const epoch = attachEpochRef.current;
      let dropped = 0;
      let totalBytes = attachedImages.reduce((sum, image) => sum + image.size, 0);

      // 整批压缩期间占住名额（见上面那条注释）；无论成功失败都要还回去
      attachInFlightRef.current += incoming.length;
      try {
        for (const file of incoming) {
          if (accepted.length >= room) {
            dropped += 1;
            continue;
          }
          try {
            const compressed = await compressImage(file);
            if (compressed.size > imageLimits.maxBytes) {
              message.error(t('knowledgeSearch.imageTooLarge', { size: formatBytes(imageLimits.maxBytes) }));
              dropped += 1;
              continue;
            }
            if (totalBytes + compressed.size > imageLimits.maxTotalBytes) {
              message.error(t('knowledgeSearch.imageTotalTooLarge', { size: formatBytes(imageLimits.maxTotalBytes) }));
              dropped += 1;
              continue;
            }
            totalBytes += compressed.size;
            accepted.push({
              file: compressed,
              objectUrl: URL.createObjectURL(compressed),
              name: (file as File).name || `image_${attachedImages.length + accepted.length + 1}`,
              size: compressed.size,
            });
          } catch (error) {
            // 解码失败（文件损坏 / 浏览器不支持）**明确提示**，不静默跳过 ——
            // 否则用户以为图附上了，检索结果却和它无关
            message.error(
              error instanceof ImageDecodeError ? t('knowledgeSearch.imageUnreadable') : t('knowledgeSearch.imageCompressFailed'),
            );
            dropped += 1;
          }
        }
      } finally {
        attachInFlightRef.current -= incoming.length;
      }

      // 压缩期间用户切了空间 / 换了检索维度：这批图属于上一个上下文，连提示一起丢掉
      if (epoch !== attachEpochRef.current) {
        accepted.forEach((image) => URL.revokeObjectURL(image.objectUrl));
        return;
      }

      if (accepted.length) setAttachedImages((prev) => [...prev, ...accepted]);
      if (dropped > 0 && accepted.length > 0) {
        message.warning(t('knowledgeSearch.imageLimitReached', { count: imageLimits.maxImages }));
      }
    },
    [attachedImages, imageLimits, t],
  );

  const handleRemoveImage = useCallback((index: number) => {
    setAttachedImages((prev) => {
      const target = prev[index];
      if (target) URL.revokeObjectURL(target.objectUrl);
      return prev.filter((_, i) => i !== index);
    });
  }, []);

  /**
   * [jonex] 以图问知识：从后端错误里同步**生效中的**上限。
   *
   * 前端内置的默认值只是「与后端默认值一致」的猜测；部署把 `IMAGE_QUERY_MAX_*`
   * 调小之后它就过期了 —— 那时用户会在前端被放到后端、再由后端拒掉。
   * 后端的超限错误里带着实际值（`error_details`），抓到就刷新本地缓存。
   */
  const syncLimitsFromError = useCallback((error: unknown) => {
    const details = (error as { errorDetails?: Record<string, unknown> } | null)?.errorDetails;
    if (!details) return;
    const next: Partial<ImageQueryLimits> = {};
    if (typeof details.max_images === 'number') next.maxImages = details.max_images;
    if (typeof details.max_bytes === 'number') next.maxBytes = details.max_bytes;
    if (typeof details.max_total_bytes === 'number') next.maxTotalBytes = details.max_total_bytes;
    if (Object.keys(next).length) setImageLimits((prev) => ({ ...prev, ...next }));
  }, []);

  // [jonex] 检索维度 = 领域空间 + 领域服务（双维度并存）：下拉即完整候选，不再按空间过滤。
  const visibleDomains = useMemo(() => domains, [domains]);

  // [jonex] 默认检索范围：保留现有选中 → 当前领域空间（=「全部知识」）→ 第一个有 KB 的候选 → 空
  //
  // 「全部知识」是**默认项**，不按有没有 KB 过滤：空间维度可能一个 KB 都没有（纯 space member
  // 的 KB 列表取自「列表口径」——我管的空间 ∪ 我被授权的 KB，见 knowledge_info_service.list，
  // 不含他所在空间下的 KB），过滤掉它下拉就留白，看起来像没加载出来。真去检索时由
  // handleSearch 的 noKbWarning 明确告知，比留白诚实。
  useEffect(() => {
    setSelectedDomain((prev) => {
      const usable = visibleDomains.filter((d) => (d.kb_ids?.length ?? 0) > 0);
      // 用户或历史选过的范围还在候选里就保留 —— 重拉数据不该顶掉用户的选择。
      // 切空间时它必然不在（服务维度只列当前空间的服务），于是落到下面的默认项。
      if (prev && usable.some((d) => d.id === prev)) return prev;
      const currentSpace = visibleDomains.find((d) => d.kind === 'space');
      if (currentSpace) return currentSpace.id;
      return usable[0]?.id ?? '';
    });
  }, [visibleDomains, global.currentSpaceId]);

  // 原文段/视频预览：统一文档查看器
  const { openDocument, viewer } = useDocumentViewer();
  // 推理链面板展开态（按 session）
  const [reasoningExpandedMap, setReasoningExpandedMap] = useState<Record<string, boolean>>({});
  // 关联原文面板展开态（按 session，默认折叠）
  const [refsExpandedMap, setRefsExpandedMap] = useState<Record<string, boolean>>({});

  // 查看原文：全部走统一弹层；视频/音频带时间锚点时定位到时间点
  const [wikiEvidence, setWikiEvidence] = useState<KnowledgeReference | null>(null);
  const openReference = useCallback(
    (ref: KnowledgeReference, loc?: KnowledgeReferenceLocation) => {
      if (ref.query_id && ref.evidence_handle && ref.kb_id) {
        setWikiEvidence(ref);
        return;
      }
      // [jonex] §image-refs P3-2: 图片引用也走统一弹层。
      // asset_url 是对象存储预签名直链，预览还是下载全由对象的 Content-Type 决定
      // （文档内嵌图资产的类型由入库侧写，历史对象可能是 octet-stream）——一旦被浏览器
      // 判成下载，链接就没法在应用内看，「下载原始文件」入口也随弹层一起丢了。
      // 引用命中 chunk + 位置字段 → 文档查看器按需加载 bbox 高亮
      const { chunkIndex, expect } = toSpanRequest(loc);
      openDocument({
        docId: ref.doc_id,
        fileName: ref.file_name,
        mediaType: ref.media_type,
        timeStart: loc?.time_start ?? null,
        timeEnd: loc?.time_end ?? null,
        // 后端 page_no 为 0-based（MinerU page_idx），PDF viewer #page=N 为 1-based
        page: loc?.page_no != null ? loc.page_no + 1 : null,
        chunkIndex,
        spanExpect: expect,
      });
    },
    [openDocument],
  );

  /**
   * [jonex] 回默认态 —— 换空间 / 换检索维度时把上一轮的检索信息整体收掉。
   *
   * 只重拉领域与历史是不够的：中间那张答案卡片是**上一个空间**检出来的，留着就会
   * 变成「新空间的历史列表 + 旧空间的回答」这种对不上的画面，看起来像新空间也命中了。
   * 输入框文字与已附图片同理，它们是上一轮的输入，留着会让下一次检索带上错空间的问题和图。
   */
  const resetSearchState = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    releaseSessionUrls();
    // 输入区那批的 objectURL 所有权没转进会话，得单独回收
    setAttachedImages((prev) => {
      prev.forEach((image) => URL.revokeObjectURL(image.objectUrl));
      return [];
    });
    attachEpochRef.current += 1;
    setActiveSearch(null);
    setActiveHistoryIndex(null);
    setQuery('');
    setSessionVote(null);
    setDislikeOpen(false);
    setVoteReason(null);
    setVoteComment('');
  }, [releaseSessionUrls]);

  // ── initial data load ──────────────────────────────────
  useEffect(() => {
    let mounted = true;
    // 空间换了，先清空再拉新数据（见 resetSearchState）
    resetSearchState();

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

        // ⚠️ 游标必须跟着列表一起回到第 1 页：`historyPage` 记的是「当前列表已加载到第几页」，
        // 只有这里（和 loadMoreHistory）会改这个列表。漏掉这一句，换空间后列表是新的第 1 页、
        // 游标却停在上个空间翻到的页数，「查看更多」直接从那一页往后拉 —— 中间的页永远看不到。
        setHistoryPage(1);

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
  }, [global.currentSpaceId, resetSearchState]);

  // ── search ─────────────────────────────────────────────
  const getSelectedKbIds = useCallback(
    (domainId?: string): string[] => {
      if (!domainId) return [];
      const domain = visibleDomains.find((d) => d.id === domainId);
      return domain?.kb_ids ?? [];
    },
    [visibleDomains],
  );

  /** 拉取某条检索历史（history_id）的权威反馈状态，覆盖本地缓存与选中态 */
  const fetchAnswerFeedback = useCallback(async (historyId: string, sessionId: string) => {
    try {
      const res = await getAnswerFeedback(historyId);
      if (!res.feedback) {
        delete voteCacheRef.current[historyId];
        delete echoedFeedbackRef.current[historyId];
        if (activeSearchRef.current?.id === sessionId) setSessionVote(null);
        return;
      }
      const f = res.feedback;
      voteCacheRef.current[historyId] = f.feedback_type;
      versionCacheRef.current[historyId] = f.version;
      if (f.feedback_reason || f.feedback_comment) {
        echoedFeedbackRef.current[historyId] = {
          reason: (f.feedback_reason as FeedbackReason) ?? null,
          comment: f.feedback_comment ?? '',
        };
      }
      if (activeSearchRef.current?.id === sessionId) setSessionVote(f.feedback_type);
    } catch {
      // 回显失败静默（保留本地缓存状态）
    }
  }, []);

  /** 统一提交回答反馈：operation_id 失败复用（幂等）+ version 单调递增（每次成功 +1），返回是否成功 */
  const submitFeedback = useCallback(
    async (
      historyId: string,
      feedbackType: SearchFeedbackType,
      reason: FeedbackReason | null,
      comment: string,
    ): Promise<boolean> => {
      if (feedbackLoading) return false;
      const sessionId = activeSearchRef.current?.id;
      const operationId =
        operationIdRef.current?.historyId === historyId ? operationIdRef.current.operationId : genUuid();
      const version = (versionCacheRef.current[historyId] ?? 0) + 1;
      setFeedbackLoading(feedbackType);
      try {
        await submitAnswerFeedbackApi({
          historyId,
          operationId,
          version,
          feedbackType,
          feedbackReason: reason,
          feedbackComment: comment,
        });
        operationIdRef.current = null;
        versionCacheRef.current[historyId] = version;
        voteCacheRef.current[historyId] = feedbackType;
        echoedFeedbackRef.current[historyId] = { reason, comment };
        if (sessionId && activeSearchRef.current?.id === sessionId) {
          setSessionVote(feedbackType);
        }
        if (feedbackType === 'dislike') {
          message.success(t('knowledgeSearch.feedbackThankYou'));
        }
        return true;
      } catch (err: any) {
        // 失败保留 operation_id 供同一次弹框内重试复用（幂等），不污染本地版本号
        message.error(err?.message || t('knowledgeSearch.operationFailed'));
        return false;
      } finally {
        setFeedbackLoading(null);
      }
    },
    [feedbackLoading, t],
  );

  /** 点踩弹框提交：必须先选原因；提交成功才关闭（失败保留弹框供幂等重试） */
  const confirmDislike = useCallback(async () => {
    if (!voteReason) {
      message.warning(t('knowledgeSearch.feedbackReasonRequired'));
      return;
    }
    const ok = await submitFeedback(dislikeHistoryId, 'dislike', voteReason, voteComment.trim());
    if (ok) setDislikeOpen(false);
  }, [voteReason, voteComment, dislikeHistoryId, submitFeedback, t]);

  const handleSearch = useCallback(
    async (
      nextQuery?: string,
      options?: {
        keepHistoryActive?: boolean;
        domainId?: string;
        strictConfig?: KnowledgeSearchStrictConfig | null;
        /** [jonex] 以图问知识：历史「重新检索」时沿用原来的图片 id（不重新识别） */
        imageIds?: string[];
        /** [jonex] 以图问知识：「重试」时复用会话里留着的那组压缩图（输入区已清空） */
        retryFiles?: Blob[];
        /** [jonex] 以图问知识：这次检索附带的**用户原话**（重新检索时从会话/历史带过来） */
        imageQuestion?: string;
        /**
         * [jonex] 以图问知识：**不取输入区当前附的图**。
         *
         * 「仅用文字检索」必须设它 —— 否则用户在这轮失败后又在输入框里附了图，
         * 这次"仅用文字"会把那些图一起拿去识别，与用户的意图正好相反。
         */
        ignoreAttached?: boolean;
      },
    ) => {
      const trimmedQuery = (nextQuery ?? query).trim();
      // [jonex] 以图问知识：**纯图检索**（输入框为空、只附图）必须能走通，
      // 原来的 `if (!trimmedQuery) return` 会把这种请求整个挡掉（F23）。
      const retryFiles = options?.retryFiles ?? [];
      const usingAttached =
        !options?.ignoreAttached &&
        !options?.imageIds?.length &&
        (attachedImages.length > 0 || retryFiles.length > 0);
      if (!trimmedQuery && !usingAttached) return;

      // [jonex] 以图问知识：**用户原话只在当次附图检索时取输入框**。
      //
      // 历史「重新检索」走的 `imageIds` 分支里，`trimmedQuery` 是历史存的**识别产出**
      // （见下面保存历史那段：query 存的是实际用于检索的文本）。拿它当"原话"会让
      // 会话卡片标题与侧栏都显示识别文本（与 query 同源，等于重复），
      // 纯图检索的历史重新检索后还会**凭空多出一条原话**。
      const userQuestion = usingAttached ? trimmedQuery : (options?.imageQuestion ?? '');

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      // 发起时的空间：等落库回包时要拿它跟当时的空间比对（见下面 .then）
      const spaceIdAtStart = currentSpaceIdRef.current;
      const sessionId = Date.now().toString();
      const domainId = options?.domainId ?? selectedDomain;
      // [jonex] 复用历史检索条件：优先用历史快照的 strictConfig，否则用当前面板配置
      const effectiveStrictConfig = options?.strictConfig ?? strictConfig;
      const kbIds = getSelectedKbIds(domainId);
      if (kbIds.length === 0) {
        message.warning(t('knowledgeSearch.noKbWarning'));
        return;
      }
      // [jonex] 选中范围里每个库的 kb_type（与 kbIds 同序）。API 层据此判断是否「只落在
      // 同一条引擎链路」以选择专用检索端点；为空/不等长时回落 /search/mix。
      const domainKbTypes = visibleDomains.find((dm) => dm.id === domainId)?.kb_types ?? [];

      // [jonex] Wiki 检索：/search/jonex_wiki 端点只接受 jonex_wiki 管线的库（后端对异管线库
      // 直接拒绝），因此这里先按 kb_type 收敛范围；类型未知（与 kb_ids 不等长）时无法判定，
      // 提示用户而不是把 lightrag/openkb 的库送去被拒。
      let scopedKbIds = kbIds;
      let kbTypes = domainKbTypes;
      if (searchMode === 'wiki') {
        if (domainKbTypes.length !== kbIds.length) {
          message.warning(t('knowledgeSearch.wikiKbTypeUnknown'));
          return;
        }
        const wikiPairs = kbIds
          .map((id, index) => [id, domainKbTypes[index]] as const)
          .filter(([, kbType]) => kbType === 'jonex_wiki');
        if (wikiPairs.length === 0) {
          message.warning(t('knowledgeSearch.noWikiKbWarning'));
          return;
        }
        scopedKbIds = wikiPairs.map(([id]) => id);
        kbTypes = wikiPairs.map(([, kbType]) => kbType);
      }
      // ── [jonex] 以图问知识：先识别、再检索（两步式）────────────────
      // 会话与输入框的处理：
      // - 输入框文字**保持现状**（下面的 setQuery(trimmedQuery)，既不，也不回填识别结果）
      //   —— 用户要能看着自己的原话判断识别对不对（D7 / F23）；
      // - 附图**清空**：它们已经移到本轮会话卡片里展示，留在输入框会让下一次检索误带上。
      const filesForRetry = usingAttached
        ? retryFiles.length
          ? retryFiles
          : attachedImages.map((image) => image.file)
        : [];
      // 重试路径的附图已经不在输入区了（当时只清 state、没 revoke，但那条会话可能已被换掉），
      // 所以重试时**现做一份**预览地址；当次检索则直接复用输入区那份。
      const localImages: SessionQueryImage[] = retryFiles.length
        ? retryFiles.map((file) => ({ localUrl: URL.createObjectURL(file), status: 'ok' as const }))
        : usingAttached
          ? attachedImages.map((image) => ({ localUrl: image.objectUrl, status: 'ok' as const }))
          : [];

      setQuery(trimmedQuery);
      if (!options?.keepHistoryActive) setActiveHistoryIndex(null);
      if (usingAttached && !retryFiles.length) {
        // 只清 state，**不 revoke** —— objectURL 的所有权移交给本轮会话（见 sessionUrlsRef）
        setAttachedImages([]);
      }
      // 上一轮会话的 objectURL 到此为止：会话已被本次替换，图片不会再被显示
      releaseSessionUrls();
      sessionUrlsRef.current = localImages
        .map((image) => image.localUrl)
        .filter((url): url is string => Boolean(url));
      setActiveSearch({
        id: sessionId,
        query: trimmedQuery,
        domainId,
        startedAt: Date.now(),
        mode: searchMode,
        wiki: searchMode === 'wiki' || (searchMode !== 'deep' && kbTypes.length > 0 && kbTypes.every((k) => k === 'jonex_wiki')),
        rawAnswer: '',
        // 有图时先进识别阶段，卡片上显示「识别图片中…」
        status: usingAttached ? 'recognizing' : 'searching',
        errorMessage: '',
        // [jonex] 检索条件快照：随本次会话保存，再次搜索时复用当时条件
        strictConfig: effectiveStrictConfig,
        queryImages: localImages,
        imageQuestion: userQuestion || undefined,
        retryFiles: filesForRetry,
      });

      // 最终交给检索接口的文本：有图时是识别产出，无图时就是用户输入
      let effectiveQuery = trimmedQuery;
      // 保存历史时要回传的图片 id（识别成功且后端给了 id 的那些）
      let imageIdsForHistory: string[] = options?.imageIds ?? [];

      try {
        if (usingAttached) {
          const recognized = await recognizeImageQuery(filesForRetry, trimmedQuery || undefined, controller.signal);
          setImageLimits(recognized.limits ?? imageLimits);
          if (controller.signal.aborted) return;

          effectiveQuery = recognized.query;
          imageIdsForHistory = recognized.images
            .filter((image) => image.status === 'ok' && image.imageId)
            .map((image) => image.imageId as string);

          setActiveSearch((prev) =>
            prev?.id === sessionId
              ? {
                  ...prev,
                  status: 'searching' as const,
                  recognized,
                  query: recognized.query,
                  queryImages: recognized.images.map((image) => ({
                    imageId: image.imageId ?? null,
                    // 失败的张不给本地预览也无所谓，位次对得上就行
                    localUrl: prev.queryImages?.[image.index]?.localUrl,
                    status: image.status,
                    errorCode: image.errorCode ?? null,
                  })),
                }
              : prev,
          );
        }

        const searchParams = {
          query: effectiveQuery,
          mode: 'mix' as const,
          topK: 5,
          domainId,
          kbIds: scopedKbIds,
          kbTypes,
          deep: searchMode === 'deep',
          wiki: searchMode === 'wiki',
          wikiThinkingMode,
          strictConfig: effectiveStrictConfig,
        };
        let streamError: Error | null = null;
        let accumulatedAnswer = '';
        let finalReferences: KnowledgeReference[] = [];
        const startTime = Date.now();

        await streamKnowledgeSearch(
          searchParams,
          {
            onProgress: (events) => {
              setActiveSearch((prev) => prev?.id === sessionId ? { ...prev, wikiProgress: events } : prev);
            },
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
              if (meta?.source === 'jonex_wiki') {
                setReasoningExpandedMap((prev) => ({ ...prev, [sessionId]: false }));
              }
              reasoningRef.current = (meta?.reasoning as any) ?? null;
              // [jonex] answer-feedback 锚点：检索响应下发检索历史 id
              const historyId = meta?.history_id;

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
              const isNoAnswer = meta?.source === 'jonex_wiki' ? false : noAnswerPatterns.some((p) => p.test(accumulatedAnswer));
              const filteredRefs = isNoAnswer ? [] : (meta?.references ?? []);

              finalReferences = filteredRefs;
              setActiveSearch((prev) =>
                prev?.id === sessionId
                  ? {
                      ...prev,
                      references: filteredRefs,
                      reasoning: meta?.reasoning ?? null,
                      elapsedMs: Date.now() - startTime,
                      source: meta?.source || prev.source,
                      historyId,
                    }
                  : prev,
              );
              // 恢复该 history_id 的本地投票缓存，再拉后端权威状态覆盖
              if (historyId) {
                const cached = voteCacheRef.current[historyId];
                setSessionVote(cached ?? null);
                void fetchAnswerFeedback(historyId, sessionId);
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
          // 历史按空间过滤：空间项取自身，服务项取其所属空间
          domainSpaceId: visibleDomains.find((d) => d.id === domainId)?.space_id,
          // ⚠️ 存的是**实际用于检索的文本**（有图时是识别产出），不是用户原话 ——
          // 历史「重新检索」要能复现这次检索。用户原话另走 imageQuestion。
          query: effectiveQuery,
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
          // [jonex] 以图问知识：图片在这一刻才由后端落盘（识别阶段只在 Redis 暂存）。
          // 过期 / 没识别成功的图会被后端跳过，历史照常保存（D11）。
          queryImageIds: imageIdsForHistory.length ? imageIdsForHistory : undefined,
          imageQuestion: userQuestion || undefined,
        })
          .then((item) => {
            // 等落库回包期间切了空间：这条是**上一个空间**的检索（落库本身是对的，
            // 它带着当时的 domainSpaceId），但不能插进新空间的历史列表
            const stillSameSpace = (spaceIdAtStart ?? undefined) === (currentSpaceIdRef.current ?? undefined);
            if (!options?.keepHistoryActive && stillSameSpace) {
              setHistory((prev) => [item, ...prev]);
              setActiveHistoryIndex(0);
            }
            // 回包里带的是**真正挂上的**图（可能比提交的少），据此刷新会话卡片。
            //
            // ⚠️ 必须**合并**而不是重建：原实现把整个列表重建成 `status:'ok'`，
            // 于是识别失败那几张的 `status:'failed'`（卡片上的「未参与检索」置灰标记）
            // 被抹掉、`localUrl` 也可能对不上 id 而丢失 —— 看起来像"图片都成功了"。
            // 以会话现有列表为底，只给真的挂上的那张补 viewUrl / 真实 id。
            if (item.queryImages?.length) {
              setActiveSearch((prev) => {
                if (prev?.id !== sessionId) return prev;
                const existing = prev.queryImages ?? [];
                const savedById = new Map(item.queryImages!.map((image) => [image.id, image]));
                const merged = existing.map((image) => {
                  const saved = image.imageId ? savedById.get(image.imageId) : undefined;
                  return saved
                    ? { ...image, imageId: saved.id, viewUrl: saved.viewUrl ?? image.viewUrl, status: 'ok' as const }
                    : image;
                });
                // 回包里多出来的（例如「重新检索」沿用 imageIds 那条路）按顺序补在末尾
                for (const saved of item.queryImages!) {
                  if (!merged.some((image) => image.imageId === saved.id)) {
                    merged.push({ imageId: saved.id, viewUrl: saved.viewUrl, status: 'ok' as const });
                  }
                }
                return { ...prev, queryImages: merged };
              });
            }
          })
          .catch(() => {
            // save failed, silently ignore
          });
      } catch (error) {
        if (controller.signal.aborted) return;
        // 超限类错误带回了后端生效中的上限，顺手刷新前端缓存（下次就不会再撞）
        syncLimitsFromError(error);
        const msg = error instanceof Error ? error.message : t('knowledgeSearch.searchFailed');
        // ⚠️ 识别失败时**不会**自动退化成纯文字检索（D2）：
        // 那样用户会以为图片参与了检索，而实际上没有。所以这里只把卡片置成错误态，
        // 由用户自己选「仅用文字检索」或「重试」。
        setActiveSearch((prev) =>
          prev?.id === sessionId ? { ...prev, status: 'error' as const, errorMessage: msg } : prev,
        );
        setThinkExpandedMap((prev) => ({ ...prev, [sessionId]: false }));
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [query, attachedImages, imageLimits, syncLimitsFromError, selectedDomain, getSelectedKbIds, visibleDomains, searchMode, wikiThinkingMode, strictConfig, fetchAnswerFeedback, t],
  );

  /**
   * [jonex] 以图问知识：「仅用文字检索」。
   *
   * 识别失败后走这条路 —— 用会话里保存的原话重新走**无图**分支。
   * 与「自动退化成纯文字」的区别在于**是用户选的**，他知道这次图片没参与。
   */
  const handleTextOnlyRetry = useCallback(
    (session: SearchSession) => {
      void handleSearch(session.imageQuestion ?? session.query, {
        domainId: session.domainId,
        strictConfig: session.strictConfig ?? undefined,
        // 用户此刻可能在输入框里又附了图 —— 「仅用文字」就是不用图，必须屏蔽掉
        ignoreAttached: true,
      });
    },
    [handleSearch],
  );

  /**
   * [jonex] 以图问知识：「重试」—— 用同一组图重走有图分支。
   *
   * 图已经在会话里留了压缩后的 Blob（原图不出浏览器，D4），所以不必让用户重选；
   * 已经识别成功过的张会命中后端的识别缓存，比第一次快得多。
   */
  const handleImageRetry = useCallback(
    async (session: SearchSession) => {
      if (!session.retryFiles?.length) return;
      await handleSearch(session.imageQuestion ?? '', {
        domainId: session.domainId,
        strictConfig: session.strictConfig ?? undefined,
        retryFiles: session.retryFiles,
      });
    },
    [handleSearch],
  );

  const handleHistoryClick = useCallback(
    async (item: KnowledgeSearchHistoryItem, index: number) => {
      setQuery(item.query);
      setActiveHistoryIndex(index);
      if (item.domainId && visibleDomains.some((d) => d.id === item.domainId)) setSelectedDomain(item.domainId);

      // [jonex] 列表**不再携带快照**（answer / references / reasoning 占响应体积 83%），
      // 点击时按 id 单取这一条。取不到（老数据无快照 / 已被清理）→ 回退为重新检索。
      const seq = ++historyClickSeqRef.current;
      setHistorySnapshotLoadingId(item.id);
      let snapshot: KnowledgeSearchHistoryItem | null = null;
      try {
        snapshot = await getKnowledgeSearchHistorySnapshot(item.id);
      } catch {
        snapshot = null;
      }
      // 连续点击时丢弃先发后到的旧响应，避免 A 的结果盖掉 B 的会话
      if (seq !== historyClickSeqRef.current) return;
      setHistorySnapshotLoadingId(null);

      // [jonex] 历史快照：有 answer/references 时直接展示历史结果，不重新检索；
      // 旧数据无快照则回退为重新检索
      const hasSnapshot = Boolean(snapshot?.answer) || (snapshot?.references?.length ?? 0) > 0;
      if (snapshot && hasSnapshot) {
        abortRef.current?.abort();
        abortRef.current = null;
        setSessionVote(null);
        // 回放的是历史快照，用的是服务端 viewUrl（不是 objectURL）→ 上一轮的本地 URL 可以收了
        releaseSessionUrls();
        const sessionId = `history-${item.id}`;
        setActiveSearch({
          id: sessionId,
          query: item.query,
          domainId: item.domainId ?? selectedDomain,
          rawAnswer: snapshot.answer ?? '',
          status: 'done',
          errorMessage: '',
          references: snapshot.references ?? [],
          reasoning: snapshot.reasoning ?? null,
          elapsedMs: snapshot.durationMs ?? snapshot.reasoning?.total_ms ?? undefined,
          // [jonex] 检索条件快照：点击历史再次搜索时复用当时的 strictConfig（来自 metadata，列表已带）
          strictConfig: item.strictConfig ?? null,
          // [jonex] answer-feedback 锚点：历史项 id 即 history_id
          historyId: item.id,
          // [jonex] 以图问知识：回放这次检索附带的图片（历史用服务端 view_url，
          // 不是本地 objectURL）；已清理的图后端不会再返回，这里自然就不显示
          queryImages: (item.queryImages ?? []).map((image) => ({
            imageId: image.id,
            viewUrl: image.viewUrl,
            status: 'ok' as const,
          })),
          imageQuestion: item.imageQuestion,
        });
        // 回显该历史的反馈状态（异步覆盖本地缓存与选中态）
        void fetchAnswerFeedback(item.id, sessionId);
        // 快照里的 raw_url 已剥离且会过期，按 doc_id/locations 重新富化
        if (snapshot.references?.length) {
          resolveKnowledgeReferences(snapshot.references)
            .then((resolved) => {
              setActiveSearch((prev) => (prev?.id === sessionId ? { ...prev, references: resolved } : prev));
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
        // [jonex] 以图问知识：**不重新识别** —— 原图字节不在前端（存的是压缩后那份，
        // 且早就随会话丢弃了），而历史里的 `query` 已经是识别产出，直接拿它检索即可。
        // 图片只沿用 id，让新这条历史也挂上同一批图。
        imageIds: item.queryImages?.map((image) => image.id),
      });
    },
    [handleSearch, releaseSessionUrls, selectedDomain, fetchAnswerFeedback, visibleDomains],
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
    // 会话没了，它占的 objectURL 也一并回收
    releaseSessionUrls();
    setActiveSearch(null);
    setActiveHistoryIndex(null);
  }, [releaseSessionUrls]);

  /** 对当前回答点击「有帮助」—— 点赞直接提交；点踩由 Popconfirm 受控打开（见 handleDislikeOpenChange） */
  const handleVoteAnswer = useCallback(
    async (feedbackType: SearchFeedbackType) => {
      if (!activeSearch || feedbackLoading) return;
      const historyId = activeSearch.historyId;
      if (!historyId) {
        message.warning(t('knowledgeSearch.noHistoryFeedback'));
        return;
      }
      if (feedbackType !== 'like') return;
      // 点赞：已是 like 则无操作（去取消），否则直接提交
      if (sessionVote === 'like') return;
      await submitFeedback(historyId, 'like', null, '');
    },
    [activeSearch, sessionVote, feedbackLoading, submitFeedback, t],
  );

  /** Popconfirm 打开/关闭：打开时校验 history_id 并预填回显原因/备注；打开视为新的提交意图起点 */
  const handleDislikeOpenChange = useCallback(
    (open: boolean) => {
      if (!open) {
        setDislikeOpen(false);
        return;
      }
      const historyId = activeSearchRef.current?.historyId;
      if (!historyId) {
        message.warning(t('knowledgeSearch.noHistoryFeedback'));
        return;
      }
      operationIdRef.current = null;
      const echoed = echoedFeedbackRef.current[historyId];
      setDislikeHistoryId(historyId);
      setVoteReason(echoed?.reason ?? null);
      setVoteComment(echoed?.comment ?? '');
      setDislikeOpen(true);
    },
    [t],
  );

  const handleDomainChange = useCallback(
    (value: string) => {
      // 换了检索维度 = 换了检索范围（领域空间 / 领域服务），上一轮结果与输入同样不再对应
      resetSearchState();
      setSelectedDomain(value);
    },
    [resetSearchState],
  );

  const handleDeleteHistory = useCallback(
    (id: string, index: number) => {
      deleteKnowledgeSearchHistory('', id)
        .then(() => {
          setHistory((prev) => prev.filter((h) => h.id !== id));
          if (activeHistoryIndex === index) setActiveHistoryIndex(null);
        })
        .catch((err: any) => message.error(err?.message || t('knowledgeSearch.operationFailed')));
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
          .catch((err: any) => message.error(err?.message || t('knowledgeSearch.operationFailed')));
      },
    });
  }, [t]);

  // [jonex] 历史"查看更多"：按页追加，去重后置底；已加载条数达总数后隐藏按钮
  const loadMoreHistory = useCallback(async () => {
    if (historyLoadingMore) return;
    const nextPage = historyPage + 1;
    const spaceId = global.currentSpaceId ?? undefined;
    setHistoryLoadingMore(true);
    try {
      const result = await getKnowledgeSearchHistory('', spaceId, nextPage, 20);
      // 等回包期间切了空间：这一页属于上一个空间，而列表已被 loadInitialData 换成新空间的第 1 页，
      // 追加进去就是新空间的历史里混进旧空间的条目，游标也会被顶着往前走
      if (spaceId !== (currentSpaceIdRef.current ?? undefined)) return;
      setHistory((prev) => {
        const existing = new Set(prev.map((h) => h.id));
        const fresh = (result.items ?? []).filter((h) => !existing.has(h.id));
        return [...prev, ...fresh];
      });
      setHistoryTotal(result.total);
      setHistoryPage(nextPage);
    } catch (err: any) {
      message.warning(err?.message || t('knowledgeSearch.operationFailed'));
    } finally {
      setHistoryLoadingMore(false);
    }
  }, [historyLoadingMore, historyPage, global.currentSpaceId, t]);

  const getDomainName = useCallback(
    (domainId?: string) => domains.find((d) => d.id === domainId)?.name ?? '',
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
      return <WelcomeEmpty onPickSuggestion={(prompt) => setQuery(prompt)} />;
    }

    const thinkParsed = parseThink(activeSearch.rawAnswer);
    return (
      <SessionCard
        // 换会话（再次检索 / 点历史）时重挂载，把卡片内部的展开态与引用抽屉一并清掉
        key={activeSearch.id}
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
        onReSearch={(session) =>
          void handleSearch(session.query, {
            domainId: session.domainId,
            strictConfig: session.strictConfig ?? undefined,
            // 沿用这次检索的图片与用户原话：**不重新识别**（原图字节不在前端），
            // 历史里的 query 已经是识别产出，直接拿它检索即可；图片只沿用 id，
            // 让新这条历史也挂上同一批图（否则「重新检索」出来的是不带图的记录）。
            imageIds: session.queryImages?.map((image) => image.imageId).filter(Boolean) as string[],
            imageQuestion: session.imageQuestion,
          })
        }
        // [jonex] 以图问知识：识别失败后的两个出口。**不做自动降级** ——
        // 自动用纯文字检索会让用户以为图片参与了（D2）。
        onTextOnlyRetry={() => handleTextOnlyRetry(activeSearch)}
        onImageRetry={() => void handleImageRetry(activeSearch)}
        onClear={handleClearSearch}
        onVote={handleVoteAnswer}
        dislikeOpen={dislikeOpen}
        voteReason={voteReason}
        voteComment={voteComment}
        onDislikeOpenChange={handleDislikeOpenChange}
        onVoteReasonChange={setVoteReason}
        onVoteCommentChange={setVoteComment}
        onDislikeConfirm={() => void confirmDislike()}
        onOpenReference={openReference}
      />
    );
  };

  // ── main render ────────────────────────────────────────
  return (
    <>
      <PageContent showCard={false}>
        <div style={{ display: 'flex', gap: 12, alignItems: 'stretch', flex: 1, minHeight: 0 }}>
          {/* 输入区放在左列内部：它只按正文区域居中，不被右侧历史栏挤偏 */}
          <div className={styles.shell}>
            {/* 结果区自己滚，输入区固定在它下方；底部留白让最后一条结果能滚到输入区上方 */}
            <div
              style={{
                flex: 1,
                minHeight: 0,
                overflowY: 'auto',
                padding: '28px 28px 180px',
                // 顶部渐隐：结果往上滚时淡出，避免文字被卡片上沿硬切。
                // 用 mask 而不是盖一层白色，才能跟着卡片的径向渐变背景走。
                maskImage: 'linear-gradient(to bottom, transparent 0, #000 24px)',
                WebkitMaskImage: 'linear-gradient(to bottom, transparent 0, #000 24px)',
              }}
            >
              {renderMainContent()}
            </div>
            {/*
              输入区浮在结果之上：绝对定位不占正文空间，结果从它下方穿过，
              位置始终贴着可视区底部（滚到结果底部时也一样）。
            */}
            <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: 0, zIndex: 20 }}>
              {/* 底部渐隐遮罩：穿到输入区下方的文字淡出，避免和输入框叠在一起糊成一片 */}
              <div
                aria-hidden
                style={{
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  bottom: 0,
                  height: 240,
                  background:
                    'linear-gradient(to bottom, rgba(255, 255, 255, 0) 0%, rgba(255, 255, 255, 0.85) 55%, #ffffff 100%)',
                  pointerEvents: 'none',
                }}
              />
              {/* 底部留 32px： composer 的投影往下铺 ~48px，贴太近会被视口边缘切掉 */}
              <div style={{ position: 'absolute', left: 28, right: 28, bottom: 32 }}>
                <SearchPanel
                  selectedDomain={selectedDomain}
                  visibleDomains={visibleDomains}
                  query={query}
                  isSearching={isSearching}
                  searchMode={searchMode}
                  onSearchModeChange={setSearchMode}
                  wikiThinkingMode={wikiThinkingMode}
                  onWikiThinkingModeChange={setWikiThinkingMode}
                  strictConfig={strictConfig}
                  onStrictConfigChange={setStrictConfig}
                  onDomainChange={handleDomainChange}
                  onQueryChange={setQuery}
                  onSearch={() => void handleSearch()}
                  onStop={handleStopSearch}
                  attachedImages={attachedImages}
                  onAttachImages={(files) => void handleAttachImages(files)}
                  onRemoveImage={handleRemoveImage}
                  maxImages={imageLimits.maxImages}
                />
              </div>
            </div>
          </div>
          <SearchHistorySidebar
            history={history}
            activeHistoryIndex={activeHistoryIndex}
            onHistoryClick={handleHistoryClick}
            onDeleteHistory={handleDeleteHistory}
            onClearHistory={handleClearHistory}
            hasMore={history.length < historyTotal}
            onLoadMore={() => void loadMoreHistory()}
            loadingMore={historyLoadingMore}
            loadingId={historySnapshotLoadingId}
          />
        </div>

        {/* 统一文档查看弹层（视频/音频可定位到时间点） */}
        {viewer}
        <WikiEvidenceDrawer reference={wikiEvidence} onClose={() => setWikiEvidence(null)}
          onOriginal={(ref, page) => {
            setWikiEvidence(null);
            openDocument({ docId: ref.doc_id, fileName: ref.file_name, mediaType: ref.media_type, page: page ?? null });
          }} />
      </PageContent>
    </>
  );
};

export default KnowledgeSearch;
