import React, { useCallback, useRef, useState } from 'react';
import { Button, Modal, message } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getShellContext, isPlatformAdmin } from '@jonex/shell-sdk';
import { getDocumentSourceSpans, getDocumentViewTicket } from '@/api/domainKnowledge';
import MarkdownContent from '@/components/MarkdownContent';
import { PdfBboxViewer } from '@/components/PdfBboxViewer';
import { shouldUseBboxViewer } from '@/components/bboxOverlay';
import type { SourceSpan } from '@/components/bboxOverlay';
import type { SpanExpect } from '@/components/documentBBox';
import { clickDownloadLink } from '@/utils/batchDocumentDownload';

// 全平台统一的文档查看器：音视频/图片/PDF/文本全部在弹层内预览，
// 视频/音频支持按时间点定位（time_start）。底层统一走 getDocumentViewTicket
// → /documents/{id}/view-ticket → /documents/{id}/raw（cos 302 预签名 / local FileResponse Range）。

export type ViewerMediaType = 'text' | 'pdf' | 'audio' | 'video' | 'image' | 'office' | 'other';

const VIDEO_EXTS = new Set(['mp4', 'avi', 'mov', 'mkv', 'flv', 'wmv', 'webm', 'm4v', 'mpg', 'mpeg', '3gp']);
const AUDIO_EXTS = new Set(['mp3', 'wav', 'flac', 'aac', 'm4a', 'ogg', 'wma', 'opus', 'amr']);
const IMAGE_EXTS = new Set(['jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'tif', 'webp']);
const PDF_EXTS = new Set(['pdf']);
const TEXT_EXTS = new Set(['txt', 'md', 'markdown', 'json', 'csv', 'log', 'xml', 'yaml', 'yml']);
const OFFICE_EXTS = new Set(['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'ods']);

/** 由文件名扩展名推断媒体类型（未显式传入 mediaType 时使用）。 */
export function inferMediaType(fileNameOrType: string): ViewerMediaType {
  const raw = (fileNameOrType || '').toLowerCase();
  const ext = raw.includes('.') ? raw.split('.').pop()! : raw;
  if (VIDEO_EXTS.has(ext)) return 'video';
  if (AUDIO_EXTS.has(ext)) return 'audio';
  if (IMAGE_EXTS.has(ext)) return 'image';
  if (PDF_EXTS.has(ext)) return 'pdf';
  if (TEXT_EXTS.has(ext)) return 'text';
  if (OFFICE_EXTS.has(ext)) return 'office';
  return 'other';
}

interface OpenOptions {
  docId: string;
  fileName: string;
  /** 媒体类型；不传则按 fileName 扩展名推断 */
  mediaType?: ViewerMediaType;
  /** 视频/音频定位起点（秒） */
  timeStart?: number | null;
  /** 视频/音频定位终点（秒） */
  timeEnd?: number | null;
  /** 文档页码（1-based；PDF 追加 #page=N 定位。后端 page_no 为 0-based，调用方须 +1） */
  page?: number | null;
  /** 引用命中的 chunk 序号；有值时 PDF 按需加载 bbox 并增强预览 */
  chunkIndex?: number | null;
  /** 引用位置中用于缩小 span 范围的字段（page/table/row/image） */
  spanExpect?: SpanExpect;
}

interface ViewerState {
  open: boolean;
  kind: ViewerMediaType;
  url: string;
  name: string;
  /** 文档 ID：下载原始文件时用于**重新签发**查看票据（票据 TTL 仅 300s，不能复用 open 时的 URL） */
  docId: string;
  timeStart?: number | null;
  timeEnd?: number | null;
  /** PDF 定位页码（1-based） */
  page?: number | null;
  chunkIndex?: number | null;
  spanExpect?: SpanExpect;
  /** 引用 bbox；加载成功才切换 PdfBboxViewer，空/失败保持 iframe */
  spans?: SourceSpan[] | null;
  /** 文本原文：Markdown 保留格式，其他文本直接显示，避免 iframe 空白。 */
  content?: string;
}

const INITIAL: ViewerState = { open: false, kind: 'other', url: '', name: '', docId: '' };

/** bbox 加载上限：超时就保持 iframe，不让弹层等后端。 */
const SOURCE_SPANS_TIMEOUT_MS = 4000;

/**
 * 统一文档查看 hook。返回 openDocument(打开查看) 和 viewer(需渲染到页面的弹层节点)。
 */
export function useDocumentViewer() {
  const { t } = useTranslation();
  const [state, setState] = useState<ViewerState>(INITIAL);
  /** 打开序号：旧文档的 bbox 响应晚到时不得覆盖新文档。 */
  const openSeq = useRef(0);
  const spansAbort = useRef<AbortController | null>(null);

  const openDocument = useCallback((opts: OpenOptions) => {
    let kind = opts.mediaType ?? inferMediaType(opts.fileName);
    // [jonex] office 查看原文定位：office 文档解析侧已持久化 preview.pdf，
    // 统一归到 PDF 预览分支（iframe + #page=N 定位）；扩展名兜底同口径。
    const extOf = (fn: string) => (fn || '').split('.').pop()?.toLowerCase() || '';
    if (kind === 'office' || (kind === 'other' && OFFICE_EXTS.has(extOf(opts.fileName)))) {
      kind = 'pdf';
    }
    const timeStart = opts.timeStart ?? null;
    const timeEnd = opts.timeEnd ?? null;
    const page = opts.page ?? null;
    const chunkIndex = opts.chunkIndex ?? null;
    const spanExpect = opts.spanExpect ?? {};
    const seq = ++openSeq.current;
    // 取消上一份文档尚未返回的请求（含 4s 超时计时）
    spansAbort.current?.abort();
    spansAbort.current = null;

    // 引用 bbox 与 view-ticket 并行取：ticket 到了就先开 iframe，
    // spans 到了再把渲染增强为 PdfBboxViewer；空/失败/超时保持 iframe。
    let spansPromise: Promise<SourceSpan[] | null> | null = null;
    if (kind === 'pdf' && chunkIndex != null) {
      const controller = new AbortController();
      spansAbort.current = controller;
      const timer = window.setTimeout(() => controller.abort(), SOURCE_SPANS_TIMEOUT_MS);
      spansPromise = getDocumentSourceSpans(opts.docId, chunkIndex, spanExpect, controller.signal)
        .then((res) => (res.degraded ? null : res.spans))
        .catch(() => null)
        .finally(() => window.clearTimeout(timer));
    }

    // md / markdown 文件：拉取原文用 MarkdownContent 渲染（保留格式）
    const isMarkdown = /\.(md|markdown)$/i.test(opts.fileName || '');
    getDocumentViewTicket(opts.docId)
      .then(async ({ url }) => {
        if (seq !== openSeq.current) return;
        const sep = url.includes('?') ? '&' : '?';
        let finalUrl = url;
        if (kind === 'video' || kind === 'audio') {
          // 音视频：302 直连对象存储（Range 流式最优），带时间锚点 fragment 定位。
          // 直连来源须在 CSP media-src 白名单内（部署项 CSP_STORAGE_ORIGIN）。
          if (timeStart != null) {
            finalUrl = `${url}#t=${timeStart}${timeEnd != null ? `,${timeEnd}` : ''}`;
          }
        } else if (kind === 'pdf' || kind === 'text' || kind === 'other') {
          // iframe 内嵌：走网关同源代理，规避跨域 CSP(frame-src) / 上游 X-Frame-Options。
          // 同源 → 与后端是 cos 还是 s3 无关，无需 CSP 白名单。
          finalUrl = `${url}${sep}proxy=1`;
          // PDF 页码定位：#page=N 是浏览器 PDF viewer 的本地 fragment（不发送到
          // 服务器），Chrome/Firefox 均支持；N 为 1-based。
          // 注意：fragment 只在 iframe 分支拼（见渲染处），PdfBboxViewer 的 url 不带。
        }
        // 文本复用同源原文代理；Markdown 保留格式，其他文本直接显示。
        if (kind === 'text' || (isMarkdown && kind === 'other')) {
          try {
            const resp = await fetch(finalUrl);
            if (resp.ok) {
              const text = await resp.text();
              setState({
                open: true, kind, url: finalUrl, name: opts.fileName, docId: opts.docId,
                timeStart, timeEnd, page, chunkIndex, spanExpect, spans: null, content: text,
              });
              return;
            }
          } catch {
            /* 拉取失败，降级 iframe */
          }
        }
        // image：302 直连对象存储即可（来源须在 CSP img-src 白名单内）
        setState({
          open: true, kind, url: finalUrl, name: opts.fileName, docId: opts.docId,
          timeStart, timeEnd, page, chunkIndex, spanExpect, spans: null,
        });
        // bbox 增强：仅当仍是本次打开且拿到有效 spans 时切换查看器。
        if (spansPromise) {
          void spansPromise.then((spans) => {
            if (seq !== openSeq.current) return;
            if (!spans || spans.length === 0) return;
            setState((s) => (s.open && s.docId === opts.docId ? { ...s, spans } : s));
          });
        }
      })
      .catch((err: any) => message.error(err?.message || t('common.openDocumentFailed')));
  }, []);

  const close = useCallback(() => {
    // 关窗即作废在途的 bbox 请求，避免响应回来后改到下一次打开的弹层。
    openSeq.current += 1;
    spansAbort.current?.abort();
    spansAbort.current = null;
    setState((s) => ({ ...s, open: false }));
  }, []);

  // [jonex] 下载原始文件：仅平台管理员可见，与 DocumentLibrary 批量下载同口径
  // （/raw 的 download 参数只有前端做门禁，网关侧不校验，故这里必须显式判权）。
  const canDownload = isPlatformAdmin(getShellContext()?.user ?? null);
  const [downloading, setDownloading] = useState(false);

  /**
   * 下载**原始文件**（不是弹窗里正在预览的那份）：
   * - office 文档的预览是解析侧 LibreOffice 转出的 preview.pdf；带 download=1 时后端
   *   跳过 preview 分支直接返回原始 xlsx/docx，由 Content-Disposition 带上原始文件名；
   * - 票据 TTL 仅 300s（RAW_VIEW_TOKEN_TTL），弹窗常开必然超时，故点击时**重新签发**，
   *   不能复用 open 阶段拿到的 state.url；
   * - 下载通道刻意不拼 proxy=1：COS 由预签名 302 直连（文件名由
   *   response-content-disposition 承载），local 走 FileResponse(attachment)，
   *   两种形态都无需网关再搬一遍字节。
   */
  const handleDownload = useCallback(async () => {
    if (!state.docId || downloading) return;
    setDownloading(true);
    try {
      const { url } = await getDocumentViewTicket(state.docId);
      const sep = url.includes('?') ? '&' : '?';
      clickDownloadLink(`${url}${sep}download=1`);
    } catch (err: any) {
      message.error(err?.message || t('common.downloadOriginalFailed'));
    } finally {
      setDownloading(false);
    }
  }, [state.docId, downloading, t]);

  const seekOnLoad = (el: HTMLVideoElement | HTMLAudioElement) => {
    if (state.timeStart != null) {
      try {
        el.currentTime = state.timeStart;
      } catch {
        /* ignore seek error */
      }
    }
  };

  // 已拉取的文本直接渲染，不再计入 iframe 布局
  const isFrame =
    (state.kind === 'pdf' || state.kind === 'text' || state.kind === 'other') && state.content == null;
  const isVideo = state.kind === 'video';
  const isImage = state.kind === 'image';
  // 下载入口放标题栏：body 是 padding:0 的 80vh iframe，再挂 footer 会把弹窗顶出视口
  const modalTitle = (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
      <span
        style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        title={state.name}
      >
        {state.name}
      </span>
      {canDownload && state.docId ? (
        <Button
          size="small"
          icon={<DownloadOutlined />}
          loading={downloading}
          onClick={handleDownload}
          // 让开右上角的关闭按钮：close 绝对定位在距右边缘 ~9px 处、宽 36px（controlHeight=36），
          // 占掉右侧 9~45px；而 header 内容右边界只在 24px 处，原先的 8px 是压在它下面的
          style={{ flexShrink: 0, marginRight: 40 }}
        >
          {t('common.downloadOriginal')}
        </Button>
      ) : null}
    </div>
  );
  // PDF 页码 fragment 只拼在 iframe 分支；PdfBboxViewer 用不带 fragment 的原始 url。
  const iframeSrc =
    state.kind === 'pdf' && state.page != null ? `${state.url}#page=${state.page}` : state.url;
  const viewer = (
    <Modal
      open={state.open}
      title={modalTitle}
      footer={null}
      width={state.kind === 'audio' ? 520 : isFrame ? 960 : isVideo || isImage ? 1100 : 820}
      onCancel={close}
      destroyOnHidden
      styles={{ body: { padding: isFrame || isVideo || isImage ? 0 : 24 } }}
    >
      {state.kind === 'video' ? (
        <video
          src={state.url}
          controls
          autoPlay
          // calc 而非 vh：弹窗顶部 offset(100) + content 上下内边距(16+16) + 标题栏(54.4)
          // ≈ 186px，留给画面 190px 才不会顶出视口；黑底同上，竖版视频的黑边不露弹窗白底
          style={{ display: 'block', width: '100%', maxHeight: 'calc(100vh - 190px)', background: '#000' }}
          onLoadedMetadata={(e) => seekOnLoad(e.currentTarget)}
        />
      ) : state.kind === 'audio' ? (
        <audio
          src={state.url}
          controls
          autoPlay
          style={{ width: '100%' }}
          onLoadedMetadata={(e) => seekOnLoad(e.currentTarget)}
        />
      ) : state.kind === 'image' ? (
        <div style={{ textAlign: 'center' }}>
          {/* 与视频同一套尺寸口径：body padding 已归零，画面顶到弹窗边 */}
          <img src={state.url} alt={state.name} style={{ maxWidth: '100%', maxHeight: 'calc(100vh - 190px)' }} />
        </div>
      ) : state.content != null ? (
        // Markdown 保留格式；其他文本按原样显示，不作为 HTML 执行。
        <div style={{ padding: 24, maxHeight: '80vh', overflow: 'auto' }}>
          {/\.(md|markdown)$/i.test(state.name) ? (
            <MarkdownContent content={state.content} />
          ) : (
            <pre style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{state.content}</pre>
          )}
        </div>
      ) : state.kind === 'pdf' && shouldUseBboxViewer(state.spans ?? []) ? (
        // 有引用 bbox：pdf.js 渲染 + 高亮；加载/渲染失败回退 iframe。
        <PdfBboxViewer
          url={state.url}
          spans={state.spans ?? []}
          targetPage={state.page ?? null}
          onError={() => setState((s) => ({ ...s, spans: null }))}
        />
      ) : (
        // pdf / text / other：iframe 内嵌预览（浏览器按 Content-Type 渲染）。
        // office 已在 openDocument 归到 pdf（解析侧 preview.pdf + #page 定位）。
        <iframe src={iframeSrc} title={state.name} style={{ width: '100%', height: '80vh', border: 'none' }} />
      )}
    </Modal>
  );

  return { openDocument, viewer, close };
}
