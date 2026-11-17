/**
 * [jonex] 列式浏览（Miller columns）的**共享视觉与交互基础件**。
 *
 * 平台上「领域本体」链路已有一套成熟的列式浏览：左侧入口列 → 点卡片向右追加一列，
 * 列间用贝塞尔曲线连起来，已出现过的节点置灰去环。这套语言本身与数据无关，
 * 所以抽到这里，供本体链路（`ColumnBrowseTab`）与 Wiki 链路（`WikiColumnBoard`）
 * 共用一份实现——两处外观完全一致，改一次两边都变，不会再各写一套然后走样。
 *
 * 只放**展示层**：卡片模型、列外壳、连线 hook、视觉常量。
 * 数据加载（调哪个接口、怎么下钻）留在各自的 Tab 里。
 */
import { useCallback, useEffect, useRef } from 'react';

// ── 视觉常量（本体链路原值，Wiki 链路沿用同一套）──────────
export const COL_WIDTH = 270;
export const COL_GAP = 54;
export const TAG_MAX_WIDTH = 96; // 标签超过此宽度截断（全文看 Tooltip）
export const THUMB_SIZE = 40; // 卡片头像边长（实例 / 关联节点列）
export const TYPE_THUMB_SIZE = 44; // 入口（类型 / 目录）列头像沿用原尺寸

// 卡片三态视觉，优先级 展开 > 悬停 > 默认（展开态被悬停时保持展开色，不降级）
export const CARD_IDLE = { border: '#e5ebf3', background: '#ffffff', shadow: '0 1px 3px rgba(39,72,116,0.03)' };
export const CARD_HOVER = { border: '#a9c7f5', background: '#f8fbff', shadow: '0 2px 8px rgba(52,120,238,0.07)' };
export const CARD_ACTIVE = { border: '#3478ee', background: '#eef4ff', shadow: '0 4px 14px rgba(52,120,238,0.16)' };

// 卡片内的气泡改浅灰底 + 深色字。只作用于列式页：Tooltip 默认挂在 body 上，
// 没法靠外层作用域隔离，所以逐处传 props 而不是改全局主题。
// color 同时决定箭头底色，文字色单独给 container（浅灰底配默认白字会看不清）。
export const CARD_TOOLTIP = {
  color: 'var(--yx-bg-muted)',
  styles: { container: { color: '#1e293b' } },
};

export const EMPTY_SET: ReadonlySet<string> = new Set<string>();

/**
 * 列内卡片模型。
 * label 主标题 / sub 副标题 / relation 右上角徽标（关系名，带方向）/ tag 名称后标签 /
 * desc 卡片底部描述一行；key 供 React 与「已出现置灰」去重使用。
 */
export interface ColumnItem {
  key: string;
  label: string;
  /** 名称下方的副标题 */
  sub?: string;
  /** 名称后的小标签 */
  tag?: string;
  /** 名称下方的描述，只占一行、悬停看全文 */
  desc?: string;
  /** 右上角徽标：关系名（带方向） */
  relation?: string;
  /** 入口列（类型 / 目录）的卡片走方形大头像 */
  isType?: boolean;
}

// ── 列间连线 + 舞台（横向滚动 + 网格底 + 滚动/缩放时重画）────

interface ColumnConnectors {
  stageRef: React.MutableRefObject<HTMLDivElement | null>;
  contentRef: React.MutableRefObject<HTMLDivElement | null>;
  svgRef: React.MutableRefObject<SVGSVGElement | null>;
  scheduleConnectors: () => void;
}

export function useColumnConnectors(columnCount: number): ColumnConnectors {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const rafRef = useRef(0);

  // 从「第 i 列选中的卡片」连到「第 i+1 列所有可见卡片」
  const drawConnectors = useCallback(() => {
    const stage = stageRef.current;
    const content = contentRef.current;
    const svg = svgRef.current;
    if (!stage || !content || !svg) return;
    const width = Math.max(content.scrollWidth, stage.clientWidth);
    const height = Math.max(content.scrollHeight, stage.clientHeight);
    svg.setAttribute('width', String(width));
    svg.setAttribute('height', String(height));
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const stageRect = stage.getBoundingClientRect();
    const cols = Array.from(content.querySelectorAll<HTMLElement>('.graph-column'));
    const NS = 'http://www.w3.org/2000/svg';
    for (let i = 0; i < cols.length - 1; i += 1) {
      const source = cols[i].querySelector<HTMLElement>('.node-card.is-selected:not([hidden])');
      if (!source) continue;
      const a = source.getBoundingClientRect();
      const x1 = a.right - stageRect.left + stage.scrollLeft;
      const y1 = a.top + a.height / 2 - stageRect.top + stage.scrollTop;
      cols[i + 1].querySelectorAll<HTMLElement>('.node-card:not([hidden])').forEach((target) => {
        const b = target.getBoundingClientRect();
        const x2 = b.left - stageRect.left + stage.scrollLeft;
        const y2 = b.top + b.height / 2 - stageRect.top + stage.scrollTop;
        const bend = Math.max(28, (x2 - x1) * 0.48);
        const path = document.createElementNS(NS, 'path');
        path.setAttribute('d', `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', 'rgba(65, 121, 207, 0.36)');
        path.setAttribute('stroke-width', '1.5');
        path.setAttribute('stroke-linecap', 'round');
        svg.appendChild(path);
      });
    }
  }, []);

  const scheduleConnectors = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(drawConnectors);
  }, [drawConnectors]);

  // 列结构变化、滚动、窗口缩放时重画连线
  useEffect(() => {
    scheduleConnectors();
  }, [columnCount, scheduleConnectors]);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    stage.addEventListener('scroll', scheduleConnectors, { passive: true });
    window.addEventListener('resize', scheduleConnectors);
    return () => {
      stage.removeEventListener('scroll', scheduleConnectors);
      window.removeEventListener('resize', scheduleConnectors);
    };
  }, [scheduleConnectors]);

  // 追加新列后自动滚动到最右
  useEffect(() => {
    const stage = stageRef.current;
    const content = contentRef.current;
    if (stage && content) {
      stage.scrollTo({ left: content.scrollWidth, behavior: 'smooth' });
    }
  }, [columnCount]);

  return { stageRef, contentRef, svgRef, scheduleConnectors };
}

export function ColumnStage({
  connectors,
  height,
  minHeight = 560,
  children,
}: {
  connectors: ColumnConnectors;
  height: string | number;
  minHeight?: number;
  children: React.ReactNode;
}) {
  return (
    <div
      ref={connectors.stageRef}
      style={{
        position: 'relative',
        height,
        // 下限取列高（minHeight 548 + 4px 边框），否则列会溢出容器、自己长出滚动条
        minHeight,
        overflow: 'auto',
        background: '#f3f6fb',
        backgroundImage:
          'linear-gradient(rgba(63,124,220,0.035) 1px, transparent 1px), linear-gradient(90deg, rgba(63,124,220,0.035) 1px, transparent 1px)',
        backgroundSize: '32px 32px',
      }}
    >
      <div
        ref={connectors.contentRef}
        style={{
          position: 'relative',
          zIndex: 1,
          display: 'flex',
          alignItems: 'flex-start',
          gap: COL_GAP,
          padding: '24px 32px 40px',
          width: 'max-content',
        }}
      >
        {children}
      </div>
      <svg
        ref={connectors.svgRef}
        style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none', zIndex: 0 }}
      />
    </div>
  );
}

/**
 * 已出现节点集合：第 i 列之前所有列出现过的 key（供第 i 列置灰，避免绕圈重复下钻）。
 * 各列加载完成后用 `onItemsChange(colKey, keys)` 上报自己渲染出的节点。
 */
export function useAppearedKeys(columnKeys: string[], itemsMap: Record<string, Set<string>>) {
  const acc = new Set<string>();
  return columnKeys.map((key) => {
    const before = new Set(acc);
    (itemsMap[key] || EMPTY_SET).forEach((k) => acc.add(k));
    return before;
  });
}
