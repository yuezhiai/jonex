import { useCallback, useRef, useState } from 'react';
import { Area } from '@ant-design/plots';
import { Empty, Flex, Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import type { PmTrendPoint } from '../types';

interface Props {
  data: PmTrendPoint[];
  loading?: boolean;
  height?: number;
  /**
   * 撑满父容器高度，用于「同排等高卡片」这类父级比图表高、否则会在卡片底部空出一截的场合。
   * 要求父级是纵向 flex 且能给出确定高度。
   */
  fill?: boolean;
  /** tooltip 里数值的名字，如「任务量」 */
  valueName?: string;
  /** tooltip 里额外展示的量，键对应 `PmTrendPoint.detail`（如 Token 侧的输入/输出/调用数） */
  detailFields?: TrendDetailField[];
}

export interface TrendDetailField {
  key: string;
  name: string;
  /** 数值格式化，缺省按原值展示 */
  format?: (value: number) => string;
}

/**
 * `fill` 模式下的高度下限。
 *
 * 低于这个值图表会挤成一团：Y 轴刻度叠在一起、x 轴标签挨着。
 * 它同时决定了**同排卡片的最小高度** —— 外层 grid 是 stretch，取两卡内容的较大者，
 * 所以这里兜住下限，等于保证同排的伙伴卡也不会被压得太矮。
 */
const FILL_MIN_HEIGHT = 264;

/** 把 #rrggbb 转成带透明度的 rgba()，让渐变跟随单一 color 来源 */
function withAlpha(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  const int = parseInt(m[1], 16);
  return `rgba(${(int >> 16) & 255}, ${(int >> 8) & 255}, ${int & 255}, ${alpha})`;
}

/**
 * 平滑面积趋势图。
 *
 * 与 `pages/DataOperations/components/TrendAreaChart.tsx` 同构、刻意复制而非共用：
 * 共用要动一个正在工作的页面，而本页需要独立的空态文案与类名命名空间。
 * 两边的图表配置都来自同一批实测结论，改动时**两边要一起改**。
 */
export default function TrendAreaChart({
  data,
  loading,
  height = 260,
  fill = false,
  valueName,
  detailFields,
}: Props) {
  const { t } = useTranslation();
  const color = '#3b82f6';

  // plots 的 height 只吃 number，百分比进不去，所以 fill 模式下量出容器实际高度再喂给它。
  // 量的是外层 wrapper：它由 flex 定高、与图表自身高度无关，故不会量到自己引起的循环。
  const [filledHeight, setFilledHeight] = useState(0);
  const observerRef = useRef<ResizeObserver | null>(null);
  const measure = useCallback(
    (el: HTMLDivElement | null) => {
      observerRef.current?.disconnect();
      observerRef.current = null;
      if (!el || !fill) return;
      const ro = new ResizeObserver(() => setFilledHeight(el.clientHeight));
      ro.observe(el);
      observerRef.current = ro;
      setFilledHeight(el.clientHeight);
    },
    [fill],
  );

  // 「全为 null」也算空：整个时间窗一次调用都没有时，G2 会画出一副只有坐标轴的空白图，
  // Y 轴还会退化成 0~1 的怪异刻度。这跟「窗口内有数据、只是某几格空」不是一回事 ——
  // 后者该断口（正确行为），前者得走空态。
  const isEmpty = !data.length || data.every((point) => point.value == null);

  // 额外量走「每项一个取值函数」，而不是再叠几条 mark：detail 里的量不进坐标系，
  // 画成线只会让一张图里出现两条量纲不同的曲线（如 token 数与调用次数）
  const detailItems = (detailFields ?? []).map((field) => (point: PmTrendPoint) => {
    const raw = point.detail?.[field.key];
    return { name: field.name, value: raw == null ? '-' : (field.format ?? String)(raw) };
  });

  return (
    // 取数中 / 无数据 / 有数据三态共用**同一块高度**：切指标或时间窗都会重新取数，若让
    // Skeleton 与 Empty 各自撑高，卡片就会跟着上下跳。高度由这层给死，内层只换不撑。
    <div ref={measure} style={fill ? { flex: 1, minHeight: FILL_MIN_HEIGHT } : { height }}>
      {/* 骨架只在「还没拿到过任何数据」时上：换指标重取期间保留上一帧，
          否则卡片会「图 → 骨架 → 图」闪一下 */}
      {loading && isEmpty ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : isEmpty ? (
        <Flex align="center" justify="center" style={{ height: '100%' }}>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('platformMonitor.empty.noData')} />
        </Flex>
      ) : fill && !filledHeight ? null : (
        <Area
          data={data}
          xField="date"
          yField="value"
          height={fill ? filledHeight : height}
          autoFit
          style={{
            shape: 'smooth',
            fill: `linear-gradient(-90deg, ${withAlpha(color, 0.02)} 0%, ${withAlpha(color, 0.45)} 100%)`,
          }}
          // v2 下平滑要 style.shape 与 line.shape 同时给，
          // 否则填充边缘平滑而描边仍是折线，两条边会分叉。
          line={{ shape: 'smooth', style: { stroke: color, lineWidth: 2 } }}
          axis={{
            // 不强制隐藏标签：锚点挤不下时交给 G2 自动抽稀，但必须禁用旋转，否则会被竖排
            x: { labelAutoRotate: false, tick: false, line: true, labelFontSize: 12 },
            y: {
              tick: false,
              line: true,
              grid: true,
              gridStroke: '#e2e8f0',
              labelFontSize: 12,
              gridStrokeOpacity: 1,
            },
          }}
          tooltip={{
            // 当前桶还没走完（右边界晚于 now）时数值还会继续涨，不标出来会被读成定稿
            title: (point: PmTrendPoint) =>
              point.closed === false
                ? `${point.date} · ${t('platformMonitor.trend.inProgress')}`
                : point.date,
            items: [
              { channel: 'y', name: valueName ?? t('platformMonitor.column.value') },
              ...detailItems,
            ],
          }}
        />
      )}
    </div>
  );
}
