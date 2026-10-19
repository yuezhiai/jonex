/**
 * 引用 bbox 覆盖层的纯函数（设计 §6）。
 *
 * bbox 值域 0–1000、top-left；`page_idx` 0-based；pdf.js 页对象 1-based。
 * 千分坐标 → 百分比定位与渲染尺寸无关：任何容器宽度/图片实际尺寸下都等比贴合。
 */
import type { CSSProperties } from 'react';

export interface SourceSpan {
  page_idx: number;
  bbox: number[];
  origin?: string;
  span_type?: string;
  ordinal?: number;
}

/** bbox（[0,1000] 千分坐标）→ 覆盖层百分比样式。 */
export function bboxPct(bbox: number[], origin?: string): CSSProperties {
  const [x0, y0raw, x1, y1raw] = bbox;
  const bottomLeft = origin === 'bottom-left';
  // bottom-left：y 轴翻转后再按 top-left 定位。
  const y0 = bottomLeft ? 1000 - y1raw : y0raw;
  const y1 = bottomLeft ? 1000 - y0raw : y1raw;
  return {
    position: 'absolute',
    left: `${x0 / 10}%`,
    top: `${y0 / 10}%`,
    width: `${Math.max((x1 - x0) / 10, 0.2)}%`,
    height: `${Math.max((y1 - y0) / 10, 0.2)}%`,
    boxSizing: 'border-box',
    borderRadius: 3,
    pointerEvents: 'none',
  };
}

/** 引用给定的页（1-based）有 span 就用它，否则落在最小 span 页；无 span 返回 null。 */
export function resolveTargetPage(spans: SourceSpan[], page: number | null): number | null {
  if (spans.length === 0) return null;
  if (page !== null && spans.some((s) => s.page_idx === page - 1)) return page;
  return Math.min(...spans.map((s) => s.page_idx)) + 1;
}

/** 目标页上 ordinal 最小的 span；该页没有则全局 ordinal 最小者。 */
export function pickPrimarySpan(
  spans: SourceSpan[],
  targetPage: number | null,
): SourceSpan | null {
  if (spans.length === 0) return null;
  const onPage = targetPage === null ? [] : spans.filter((s) => s.page_idx === targetPage - 1);
  const pool = onPage.length > 0 ? onPage : spans;
  return pool.reduce((best, s) => ((s.ordinal ?? 0) < (best.ordinal ?? 0) ? s : best));
}

/** 有 bbox 才用查看器，否则前端回退 iframe。 */
export function shouldUseBboxViewer(spans: SourceSpan[]): boolean {
  return spans.length > 0;
}
