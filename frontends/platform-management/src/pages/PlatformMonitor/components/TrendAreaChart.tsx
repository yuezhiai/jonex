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
  /** tooltip 里主数值的格式化（如成功率补 `%` 并收位）；缺省按原值展示 */
  valueFormat?: (value: number) => string;
  /** tooltip 里额外展示的量，键对应 `PmTrendPoint.detail`（如 Token 侧的输入/输出/调用数） */
  detailFields?: TrendDetailField[];
  /**
   * Y 轴刻度的格式化，缺省按原值展示。
   *
   * 与 tooltip 的 `valueFormat` 分开传：tooltip 要精确值（99.75%），轴上只要量级（99.4% / 1.3k），
   * 同一函数会要么让轴变宽、要么让 tooltip 丢精度。
   */
  yFormat?: (value: number) => string;
  /**
   * tooltip 是否保留「主数值」那一行（缺省保留）。
   *
   * 关掉用于「浮框要按固定几行展示各量」的场合 —— 否则第一行永远是当前所选指标，
   * 与 detail 里的同名字段要么重复、要么顺序跟着指标漂移。
   */
  showValueRow?: boolean;
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
  yFormat,
  showValueRow = true,
  valueFormat,
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

  // 后端对三个粒度都补网格：整窗无数据时也会返回「每个桶一个点、value 全为 null」。
  // 产品口径（2026-10-30）：整窗无数据同样按 0 展示，画一条贴底 0 基线。
  // 因此空态只看「数组是否为空」—— 空数组只会在接口降级 / 未取到数据时出现。
  const isEmpty = !data.length;
  // 是否已有任何真实数值：只用于决定首次取数时要不要上骨架（刷新时保留上一帧）。
  const hasValue = data.some((point) => point.value != null);

  // 空桶（接口 value=null）在**消费端**兜成 0，落到 0 基线画出来。
  // 不用 connectNulls 跨过缺口 —— 跨过去等于把「这格无数据」画成「有数据」；
  // 兜 0 至少位置是对的（沉到底部），一眼能看出这一段是空的。
  const chartData = data.map((point) => (point.value == null ? { ...point, value: 0 } : point));

  // 额外量走「每项一个取值函数」，而不是再叠几条 mark：detail 里的量不进坐标系，
  // 画成线只会让一张图里出现两条量纲不同的曲线（如 token 数与调用次数）
  const detailItems = (detailFields ?? []).map((field) => (point: PmTrendPoint) => {
    const raw = point.detail?.[field.key];
    return { name: field.name, value: raw == null ? '-' : (field.format ?? String)(raw) };
  });

  // 主数值同样走取值函数：`channel: 'y'` 会把原始值直接丢给 tooltip（成功率是 99.7529...），
  // 补 `%` 与收位只能在展示层做
  const valueItem = (point: PmTrendPoint) => ({
    name: valueName ?? t('platformMonitor.column.value'),
    value: point.value == null ? '-' : (valueFormat ?? String)(point.value),
  });

  return (
    // 取数中 / 无数据 / 有数据三态共用**同一块高度**：切指标或时间窗都会重新取数，若让
    // Skeleton 与 Empty 各自撑高，卡片就会跟着上下跳。高度由这层给死，内层只换不撑。
    <div ref={measure} style={fill ? { flex: 1, minHeight: FILL_MIN_HEIGHT } : { height }}>
      {/* 骨架只在「还没拿到过任何数据」时上：换指标重取期间保留上一帧，
          否则卡片会「图 → 骨架 → 图」闪一下 */}
      {loading && !hasValue ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : isEmpty ? (
        <Flex align="center" justify="center" style={{ height: '100%' }}>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('platformMonitor.empty.noData')} />
        </Flex>
      ) : fill && !filledHeight ? null : (
        <Area
          data={chartData}
          xField="date"
          yField="value"
          height={fill ? filledHeight : height}
          // 跨天的小时桶刻度带日期（"10-23 15:00"），末位刻度居中对齐后会顶出画布右边缘被切掉
          paddingRight={28}
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
              // 轴单位跟 tooltip 走同一套量级口径（k / M、% / ms），否则同一张图里两种写法
              labelFormatter: (value: number) => (yFormat ? yFormat(Number(value)) : String(value)),
            },
          }}
          tooltip={{
            // 当前桶还没走完（右边界晚于 now）时数值还会继续涨，不标出来会被读成定稿
            title: (point: PmTrendPoint) =>
              point.closed === false
                ? `${point.date} · ${t('platformMonitor.trend.inProgress')}`
                : point.date,
            items: showValueRow ? [valueItem, ...detailItems] : detailItems,
          }}
        />
      )}
    </div>
  );
}
