import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { Button, Empty, Space, Tag, theme } from 'antd';
import { ExpandOutlined, MinusOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { DgGraphEdge, DgGraphNode, DgLineage } from '../types';
import { LINEAGE_LAYOUT } from '../constant';
import { clampPan, computeFitView, layoutLineage, lineageEdgePath, zoomAt } from '../utils';
import type { LineageLayout, ViewTransform } from '../utils';

/** 未知节点类型的描边色（与 chunk 同色），保证新类型也不会画丢。 */
const DEFAULT_NODE_COLOR = '#8c8c8c';

/** 节点类型 → 描边色。 */
const NODE_COLORS: Record<string, string> = {
  document: '#1677ff',
  data_source: '#722ed1',
  ontology_entity: '#13c2c2',
  search_citation: '#fa8c16',
  chunk: DEFAULT_NODE_COLOR,
};

const ARROW_MARKER_ID = 'dg-lineage-arrow';

/**
 * 列标题文案（与 `LINEAGE_LAYOUT.columnX` 的四列一一对应）。
 * 第 2 列同时容纳实体与检索引用（search_citation），故称「下游」而非「本体」。
 */
const COLUMN_LABEL_KEYS = [
  'dataGovernance.lineage.colSource',
  'dataGovernance.lineage.colDocument',
  'dataGovernance.lineage.colDownstream',
  'dataGovernance.lineage.colChunk',
];

/** 按钮 / 滚轮的缩放步进（按钮一次 1.2 倍，滚轮按 deltaY 连续）。 */
const ZOOM_STEP = 1.2;
const WHEEL_ZOOM_SENSITIVITY = 0.0015;

interface DragState {
  pointerId: number;
  startX: number;
  startY: number;
  originTx: number;
  originTy: number;
}

/** 全角/CJK 记 2 个显示单位，ASCII 记 1。 */
function isWideChar(codePoint: number): boolean {
  return (
    (codePoint >= 0x1100 && codePoint <= 0x115f) || // Hangul Jamo
    (codePoint >= 0x2e80 && codePoint <= 0xa4cf) || // CJK 部首/假名/汉字
    (codePoint >= 0xac00 && codePoint <= 0xd7a3) || // Hangul 音节
    (codePoint >= 0xf900 && codePoint <= 0xfaff) || // CJK 兼容汉字
    (codePoint >= 0xfe10 && codePoint <= 0xfe19) || // 竖排标点
    (codePoint >= 0xfe30 && codePoint <= 0xfe6f) || // CJK 兼容形式
    (codePoint >= 0xff00 && codePoint <= 0xff60) || // 全角形式
    (codePoint >= 0xffe0 && codePoint <= 0xffe6) || // 全角符号
    codePoint >= 0x1f000 // emoji 等辅助平面符号
  );
}

/**
 * 按**显示宽度**截断标签，避免中文在 200px 节点框里溢出或被裁。
 *
 * 用 `Array.from` 按码点切分，不会把代理对（emoji）切成半个字符。
 * 完整文本由调用方的 `<title>` 提供。
 */
function truncateLabel(label: string): string {
  const chars = Array.from(label);
  let units = 0;
  for (let index = 0; index < chars.length; index += 1) {
    const codePoint = chars[index].codePointAt(0) ?? 0;
    units += isWideChar(codePoint) ? 2 : 1;
    if (units > LINEAGE_LAYOUT.maxLabelUnits) {
      return `${chars.slice(0, index).join('')}…`;
    }
  }
  return label;
}

/**
 * 轻量 SVG 血缘图（固定高度视口 + 自动适配 + 缩放平移）。
 *
 * 不做力导向布局：`layoutLineage` 已按「来源 → 文档 → 下游 → 切片」分列，
 * 边的走线由 `lineageEdgePath` 统一决定（相邻直线 / 同列弧线 / 跨列顶部总线）。
 * 超过 `DG_MAX_RENDER_NODES` 的节点由布局层折叠，底部用 Tag 提示 truncated
 * 与 hiddenCount。
 *
 * 视图变换只改 SVG 内 `<g>` 的 `transform`，布局仍由 `layoutLineage` 记忆化，
 * 拖拽 / 缩放不会重算节点位置；图本身不再撑高抽屉。
 */
export default function LineageGraph({ lineage }: { lineage: DgLineage }) {
  const { t } = useTranslation();
  const { token } = theme.useToken();
  // 布局按 lineage 记忆化：拖拽/缩放只改 transform，不重跑布局。
  const layout = useMemo(() => layoutLineage(lineage), [lineage]);
  // 边的 path 只随布局/边集合变化；拖拽与缩放每帧都只改 transform。
  const edges = useMemo(
    () =>
      (lineage.edges ?? []).flatMap((edge: DgGraphEdge, index: number) => {
        const path = lineageEdgePath(layout, edge);
        return path ? [{ key: `${edge.source}-${edge.target}-${index}`, path }] : [];
      }),
    [layout, lineage.edges],
  );
  const hasNodes = layout.visibleNodes.length > 0;
  // 只在有节点的列渲染标题，避免窄图右侧出现孤立文案。
  const columnsWithNodes = useMemo(() => {
    const columns = new Set<number>();
    Object.values(layout.positions).forEach((position) => columns.add(position.column));
    return Array.from(columns).sort((a, b) => a - b);
  }, [layout]);
  const zoomHintId = useId();

  const viewportRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const [view, setView] = useState<ViewTransform>({ scale: 1, tx: 0, ty: 0 });
  const [dragging, setDragging] = useState(false);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });

  /** 适配视口：整图缩放到视口内并居中（同时是「重置」）。 */
  const fit = useCallback(() => {
    const element = viewportRef.current;
    if (!element) return;
    setView(
      computeFitView(
        { width: layout.width, height: layout.height },
        { width: element.clientWidth, height: element.clientHeight },
      ),
    );
  }, [layout]);

  // 容器尺寸观察：抽屉宽度变化 / 窗口缩放时重新适配。
  // 放在 layout effect 里：测量发生在浏览器绘制前，首帧不会先按 100% 画一次再跳变。
  useLayoutEffect(() => {
    if (!hasNodes) return undefined;
    const element = viewportRef.current;
    if (!element) return undefined;
    const measure = () => {
      setViewportSize({ width: element.clientWidth, height: element.clientHeight });
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasNodes]);

  // lineage 变化（新图）或视口尺寸变化时自动 fit；按钮「适配」复用同一个函数。
  // 注意：resize 会丢弃用户当前的缩放/平移并重新 fit，这是刻意行为——抽屉宽度
  // 变化后旧视图往往已不适用，回到「整图可见」比保留一个跑偏的视图更可预期。
  useLayoutEffect(() => {
    if (!hasNodes) return;
    fit();
  }, [fit, hasNodes, viewportSize.width, viewportSize.height]);

  // 滚轮缩放：以鼠标位置为锚点，阻止抽屉/页面跟着滚动。
  useEffect(() => {
    if (!hasNodes) return undefined;
    const element = viewportRef.current;
    if (!element) return undefined;
    const handleWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      const anchorX = event.clientX - rect.left;
      const anchorY = event.clientY - rect.top;
      // 归一化 deltaMode：0=像素、1=行（Firefox）、2=页，否则行/页模式下缩放几乎不动。
      const delta =
        event.deltaMode === 1
          ? event.deltaY * 16
          : event.deltaMode === 2
            ? event.deltaY * element.clientHeight
            : event.deltaY;
      setView((previous) =>
        zoomAt(previous, previous.scale * Math.exp(-delta * WHEEL_ZOOM_SENSITIVITY), anchorX, anchorY),
      );
    };
    element.addEventListener('wheel', handleWheel, { passive: false });
    return () => element.removeEventListener('wheel', handleWheel);
  }, [hasNodes]);

  /** 按钮缩放：以视口中心为锚点。 */
  const zoomBy = (factor: number) => {
    const element = viewportRef.current;
    if (!element) return;
    setView((previous) =>
      zoomAt(previous, previous.scale * factor, element.clientWidth / 2, element.clientHeight / 2),
    );
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originTx: view.tx,
      originTy: view.ty,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setDragging(true);
  };

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    // 兜底：某些场景（窗口失焦、指针捕获丢失）收不到 pointerup，
    // 按键已抬起时直接按拖拽结束处理，避免「粘手」。
    if (event.buttons === 0) {
      endDrag(event);
      return;
    }
    // ⚠ React 会在 handler 返回后把 currentTarget 置空；updater 在渲染期执行，
    // 必须在进入 setState 前把尺寸取成数字。
    const viewport = {
      width: event.currentTarget.clientWidth,
      height: event.currentTarget.clientHeight,
    };
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    setView((previous) =>
      clampPan(
        { scale: previous.scale, tx: drag.originTx + dx, ty: drag.originTy + dy },
        layout,
        viewport,
      ),
    );
  };

  if (!hasNodes) {
    return <Empty description={t('dataGovernance.lineage.empty')} />;
  }

  const edgeStroke = token.colorBorder;
  const countsText = t('dataGovernance.lineage.counts', {
    entities: lineage.counts.entities,
    relations: lineage.counts.relations,
    citations: lineage.counts.citations,
    chunks: lineage.counts.chunks ?? '—',
  });
  const percent = Math.round(view.scale * 100);

  return (
    <div>
      <div
        ref={viewportRef}
        className={`yx-dg-lineage-viewport${dragging ? ' is-dragging' : ''}`}
        aria-describedby={zoomHintId}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onLostPointerCapture={endDrag}
      >
        <div
          className="yx-dg-lineage-toolbar"
          onPointerDown={(event) => event.stopPropagation()}
        >
          <Button
            size="small"
            type="text"
            icon={<MinusOutlined />}
            aria-label={t('dataGovernance.lineage.zoomOut')}
            title={t('dataGovernance.lineage.zoomOut')}
            onClick={() => zoomBy(1 / ZOOM_STEP)}
          />
          <span className="yx-dg-lineage-zoom-value" aria-label={`${percent}%`}>
            {percent}%
          </span>
          <Button
            size="small"
            type="text"
            icon={<PlusOutlined />}
            aria-label={t('dataGovernance.lineage.zoomIn')}
            title={t('dataGovernance.lineage.zoomIn')}
            onClick={() => zoomBy(ZOOM_STEP)}
          />
          <Button
            size="small"
            type="text"
            icon={<ExpandOutlined />}
            aria-label={t('dataGovernance.lineage.fit')}
            title={t('dataGovernance.lineage.fit')}
            onClick={fit}
          />
          <span id={zoomHintId} className="yx-dg-lineage-sr-only">
            {t('dataGovernance.lineage.zoomHint')}
          </span>
        </div>

        <svg
          role="img"
          aria-label={`${t('dataGovernance.lineage.title')}: ${countsText}`}
          width="100%"
          height="100%"
          style={{ display: 'block' }}
        >
          <defs>
            <marker
              id={ARROW_MARKER_ID}
              viewBox="0 0 8 8"
              refX={7}
              refY={4}
              markerWidth={7}
              markerHeight={7}
              orient="auto"
            >
              <path d="M 0 0 L 8 4 L 0 8 z" fill={edgeStroke} />
            </marker>
          </defs>

          <g transform={`translate(${view.tx} ${view.ty}) scale(${view.scale})`}>
            {edges.map((edge) => (
              <path
                key={edge.key}
                d={edge.path}
                fill="none"
                stroke={edgeStroke}
                strokeWidth={1.2}
                markerEnd={`url(#${ARROW_MARKER_ID})`}
              />
            ))}

            {/* 列标题画在边之后（总线穿过时文字仍在上层），基线 y 由
                LINEAGE_LAYOUT.columnLabelY 与 busY 同源维护，已位于总线之上。
                背景色描边光晕仅用于文字边缘抗锯齿，它依赖「视口背景 ==
                token.colorBgContainer」（见 index.scss 的 .yx-dg-lineage-viewport），
                换主题或改视口背景时需同步检查。 */}
            {columnsWithNodes.map((column) => (
              <text
                key={`column-${column}`}
                x={LINEAGE_LAYOUT.columnX[column] + LINEAGE_LAYOUT.nodeWidth / 2}
                y={LINEAGE_LAYOUT.columnLabelY}
                textAnchor="middle"
                dominantBaseline="middle"
                fontSize={12}
                fill={token.colorTextSecondary}
                paintOrder="stroke"
                stroke={token.colorBgContainer}
                strokeWidth={4}
                strokeLinejoin="round"
              >
                {t(COLUMN_LABEL_KEYS[column])}
              </text>
            ))}

            {layout.visibleNodes.map((node: DgGraphNode) => {
              const pos: LineageLayout['positions'][string] = layout.positions[node.id];
              const color = NODE_COLORS[node.type] ?? DEFAULT_NODE_COLOR;
              return (
                <g key={node.id}>
                  <title>{node.label}</title>
                  <rect
                    x={pos.x}
                    y={pos.y}
                    width={LINEAGE_LAYOUT.nodeWidth}
                    height={LINEAGE_LAYOUT.nodeHeight}
                    rx={6}
                    fill={token.colorBgContainer}
                    stroke={color}
                    strokeWidth={1.5}
                  />
                  {/* 左侧 4px 语义色竖条：比整框描边更轻，节点类型一眼可辨。 */}
                  <rect
                    x={pos.x + 2}
                    y={pos.y + 2}
                    width={4}
                    height={LINEAGE_LAYOUT.nodeHeight - 4}
                    rx={2}
                    fill={color}
                  />
                  <text
                    x={pos.x + LINEAGE_LAYOUT.nodeWidth / 2}
                    y={pos.y + LINEAGE_LAYOUT.nodeHeight / 2}
                    textAnchor="middle"
                    dominantBaseline="middle"
                    fontSize={12}
                    fill={token.colorText}
                  >
                    {truncateLabel(node.label)}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>
      </div>

      <Space wrap size={4} style={{ marginTop: 8 }}>
        <Tag>{countsText}</Tag>
        {lineage.truncated.entities && (
          <Tag color="gold">
            {t('dataGovernance.lineage.truncatedEntities', {
              count: LINEAGE_LAYOUT.truncatedEntities,
            })}
          </Tag>
        )}
        {lineage.truncated.relations && (
          <Tag color="gold">
            {t('dataGovernance.lineage.truncatedRelations', {
              count: LINEAGE_LAYOUT.truncatedRelations,
            })}
          </Tag>
        )}
        {lineage.truncated.chunks && (
          <Tag color="gold">
            {t('dataGovernance.lineage.truncatedChunks', {
              count: LINEAGE_LAYOUT.truncatedChunks,
            })}
          </Tag>
        )}
        {layout.hiddenCount > 0 && (
          <Tag>{t('dataGovernance.lineage.collapsed', { count: layout.hiddenCount })}</Tag>
        )}
      </Space>
    </div>
  );
}
