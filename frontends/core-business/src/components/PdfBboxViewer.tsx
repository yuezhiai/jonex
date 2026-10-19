/**
 * PDF 虚拟滚动预览 + bbox 高亮（ESWiki `PdfPreview.tsx` 的移植）。
 *
 * - 骨架：页宽适配容器（上限 1.8 倍），逐页算绝对 {top,height}，撑出滚动条；
 * - 虚拟渲染：只画可视区 ±1.5/2.5 屏，滚出即卸载（回来自动重画）；
 * - bbox：千分坐标 → 百分比定位（等比）；全部 spans 蓝框常驻，
 *   主 span 红框脉冲，打开后平滑滚动至目标页的主 span。
 *
 * bbox 值域 0–1000、top-left；`page_idx` 0-based；pdf.js 页对象 1-based。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, UIEvent } from 'react';
import * as pdfjsLib from 'pdfjs-dist';
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

import { bboxPct, pickPrimarySpan, resolveTargetPage } from './bboxOverlay';
import type { SourceSpan } from './bboxOverlay';
import './bboxOverlay.css';

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorker;

const PAGE_GAP = 16;
const PAD = 40;

const SPAN_STYLE: CSSProperties = {
  border: '2px solid rgba(59,130,246,.9)',
  background: 'rgba(59,130,246,.15)',
};

const PRIMARY_STYLE: CSSProperties = {
  border: '3px solid #ef4444',
  background: 'rgba(239,68,68,.20)',
  boxShadow: '0 0 15px rgba(239,68,68,.7)',
};

interface PageBox {
  /** 1 起页码 */
  id: number;
  width: number;
  height: number;
  top: number;
  viewport: pdfjsLib.PageViewport;
  page: pdfjsLib.PDFPageProxy;
}

interface PdfPageProps {
  box: PageBox;
  spans: SourceSpan[];
  /** 该页的主 span（其他页为 null） */
  primary: SourceSpan | null;
  pulse: boolean;
  onError: () => void;
}

function PdfPage({ box, spans, primary, pulse, onError }: PdfPageProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [drawn, setDrawn] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let dead = false;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(box.width * dpr);
    canvas.height = Math.floor(box.height * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const task = box.page.render({
      canvasContext: ctx,
      viewport: box.viewport,
      transform: [dpr, 0, 0, dpr, 0, 0],
    });
    task.promise
      .then(() => {
        if (!dead) setDrawn(true);
      })
      .catch((e: unknown) => {
        const name = (e as { name?: string })?.name;
        if (!dead && name !== 'RenderingCancelledException') {
          console.warn('pdf render failed', e);
          onError();
        }
      });
    return () => {
      dead = true;
      task.cancel();
    };
  }, [box, onError]);

  return (
    <div
      style={{
        position: 'relative',
        width: box.width,
        height: box.height,
        background: '#fff',
        boxShadow: '0 1px 4px rgba(0,0,0,.15)',
      }}
    >
      {!drawn ? (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#94a3b8',
            fontSize: 12,
          }}
        >
          第 {box.id} 页渲染中…
        </div>
      ) : null}
      <canvas
        ref={canvasRef}
        style={{ display: 'block', width: box.width, height: box.height }}
      />
      {drawn ? (
        <div style={{ position: 'absolute', inset: 0 }}>
          {spans.map((s, i) => (
            <div key={i} style={{ ...bboxPct(s.bbox, s.origin), ...SPAN_STYLE }} />
          ))}
          {primary ? (
            <div
              style={{ ...bboxPct(primary.bbox, primary.origin), ...PRIMARY_STYLE }}
              className={pulse ? 'jonex-bbox-pulse' : undefined}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

const VIEWPORT: CSSProperties = {
  height: 'min(70vh, 700px)',
  overflowY: 'auto',
  background: '#e5e7eb',
  borderRadius: 8,
  position: 'relative',
};

// 错误提示作为覆盖层渲染：滚动容器必须常驻，否则 url 变化时
// load effect 拿到 null 的 scrollRef，重挂载后也不会重跑（实例被永久卡死）。
const ERROR_OVERLAY: CSSProperties = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 24,
  textAlign: 'center',
  color: '#b91c1c',
  background: 'rgba(255,255,255,.92)',
};

export interface PdfBboxViewerProps {
  url: string;
  spans: SourceSpan[];
  /** 引用页（1-based）；该页没有 span 时自动回退到最小页 */
  targetPage: number | null;
  /** pdf.js 加载或渲染失败时调用（调用方据此回退 iframe） */
  onError: () => void;
}

export function PdfBboxViewer({ url, spans, targetPage, onError }: PdfBboxViewerProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pdfRef = useRef<pdfjsLib.PDFDocumentProxy | null>(null);
  const pulseTimer = useRef<number | undefined>(undefined);
  const located = useRef('');
  const notified = useRef(false);
  const onErrorRef = useRef(onError);
  const [pages, setPages] = useState<PageBox[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [containerH, setContainerH] = useState(600);
  const [pulse, setPulse] = useState(false);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  // 加载/渲染失败只通知一次，避免逐页渲染失败刷屏
  const fail = useCallback(() => {
    if (notified.current) return;
    notified.current = true;
    onErrorRef.current?.();
  }, []);

  const validSpans = useMemo(
    () => spans.filter((s) => Array.isArray(s.bbox) && s.bbox.length === 4),
    [spans],
  );

  const byPage = useMemo(() => {
    const m = new Map<number, SourceSpan[]>();
    for (const s of validSpans) {
      const arr = m.get(s.page_idx) ?? [];
      arr.push(s);
      m.set(s.page_idx, arr);
    }
    for (const arr of m.values()) arr.sort((a, b) => (a.ordinal ?? 0) - (b.ordinal ?? 0));
    return m;
  }, [validSpans]);

  const target = useMemo(
    () => resolveTargetPage(validSpans, targetPage),
    [validSpans, targetPage],
  );
  const primary = useMemo(() => pickPrimarySpan(validSpans, target), [validSpans, target]);

  useEffect(() => {
    const scroll = scrollRef.current;
    let dead = false;
    notified.current = false;
    setPages([]);
    setError(null);
    setPulse(false);
    const old = pdfRef.current;
    pdfRef.current = null;
    if (old) void old.destroy();
    if (!scroll || !url) return;
    setContainerH(scroll.clientHeight || 600);
    void (async () => {
      try {
        const doc = await pdfjsLib.getDocument(url).promise;
        if (dead) {
          void doc.destroy();
          return;
        }
        pdfRef.current = doc;
        const cw = (scroll.clientWidth || 800) - PAD;
        // 页宽适配：以第 1 页为基准，最多放大 1.8 倍，不超出容器
        const base = (await doc.getPage(1)).getViewport({ scale: 1 });
        const fit = Math.min(cw / base.width, 1.8);
        const boxes: PageBox[] = [];
        let top = PAGE_GAP;
        for (let i = 1; i <= doc.numPages; i++) {
          const page = await doc.getPage(i);
          const viewport = page.getViewport({ scale: fit });
          boxes.push({
            id: i,
            width: viewport.width,
            height: viewport.height,
            top,
            viewport,
            page,
          });
          top += viewport.height + PAGE_GAP;
        }
        if (!dead) setPages(boxes);
      } catch (e) {
        if (!dead) {
          setError(`PDF 加载失败：${e instanceof Error ? e.message : String(e)}`);
          fail();
        }
      }
    })();
    return () => {
      dead = true;
      const d = pdfRef.current;
      pdfRef.current = null;
      if (d) void d.destroy();
    };
  }, [url, fail]);

  useEffect(() => () => window.clearTimeout(pulseTimer.current), []);

  // 主 span 定位（骨架就绪后一次；同 url+span 不重复定位）
  useEffect(() => {
    if (!pages.length || !primary) return;
    const key = `${url}|${target}|${primary.bbox.join(',')}`;
    if (located.current === key) return;
    const scroll = scrollRef.current;
    const box = pages.find((p) => p.id === primary.page_idx + 1);
    if (!scroll || !box) return;
    located.current = key;
    const bboxTop =
      primary.origin === 'bottom-left' ? 1000 - primary.bbox[3] : primary.bbox[1];
    const targetY = box.top + (bboxTop / 1000) * box.height - containerH / 3;
    scroll.scrollTo({ top: Math.max(0, targetY), behavior: 'smooth' });
    setPulse(true);
    window.clearTimeout(pulseTimer.current);
    pulseTimer.current = window.setTimeout(() => setPulse(false), 3000);
  }, [pages, primary, target, containerH, url]);

  const visible = useMemo(() => {
    const startY = scrollTop - containerH * 1.5;
    const endY = scrollTop + containerH * 2.5;
    return pages.filter((p) => p.top + p.height >= startY && p.top <= endY);
  }, [pages, scrollTop, containerH]);

  const last = pages[pages.length - 1];
  const totalH = last ? last.top + last.height + PAGE_GAP : 0;

  return (
    <div
      ref={scrollRef}
      style={VIEWPORT}
      onScroll={(e: UIEvent<HTMLDivElement>) => setScrollTop(e.currentTarget.scrollTop)}
    >
      {error ? (
        <div style={ERROR_OVERLAY}>{error}</div>
      ) : pages.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '60px 0', color: '#64748b' }}>
          PDF 加载中…
        </div>
      ) : (
        <div style={{ height: totalH, position: 'relative' }}>
          {visible.map((p) => (
            <div
              key={p.id}
              style={{
                position: 'absolute',
                top: p.top,
                left: 0,
                right: 0,
                height: p.height,
                display: 'flex',
                justifyContent: 'center',
              }}
            >
              <PdfPage
                box={p}
                spans={byPage.get(p.id - 1) ?? []}
                primary={primary && primary.page_idx === p.id - 1 ? primary : null}
                pulse={pulse}
                onError={fail}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default PdfBboxViewer;
