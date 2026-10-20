import { useCallback, useRef, useState } from 'react';
import { Area } from '@ant-design/plots';
import { Empty, Flex, Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DoTrendPoint } from '../types';

interface Props {
  data: DoTrendPoint[];
  loading?: boolean;
  height?: number;
  /**
   * 撑满父容器高度，用于「同排等高卡片」这类父级比图表高、否则会在卡片底部空出一截的场合。
   * 要求父级是纵向 flex 且能给出确定高度（见 DataAssets 的 .yx-do-two-col--wide-right）。
   */
  fill?: boolean;
  color?: string;
  /** tooltip 里数值的名字，如「接入量」 */
  valueName?: string;
}

/** 把 #rrggbb 转成带透明度的 rgba()，让渐变跟随单一 color 来源 */
function withAlpha(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  const int = parseInt(m[1], 16);
  return `rgba(${(int >> 16) & 255}, ${(int >> 8) & 255}, ${int & 255}, ${alpha})`;
}

/**
 * 平滑面积趋势图。
 * 配置来自实测：v2 下平滑要 style.shape 与 line.shape 同时给，
 * 否则填充边缘平滑而描边仍是折线，两条边会分叉。
 */
export default function TrendAreaChart({
  data,
  loading,
  height = 260,
  fill = false,
  color = '#3b82f6',
  valueName,
}: Props) {
  const { t } = useTranslation();

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

  const isEmpty = !data.length;

  return (
    // 与 PlatformMonitor 那份同一处改动：三态共用一块高度，切指标重取时卡片不上下跳
    <div ref={measure} style={fill ? { flex: 1, minHeight: 0 } : { height }}>
      {/* 骨架只在「还没拿到过任何数据」时上，换指标重取期间保留上一帧，避免闪一下 */}
      {loading && isEmpty ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : isEmpty ? (
        <Flex align="center" justify="center" style={{ height: '100%' }}>
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('dataOperations.empty.noData')} />
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
          line={{ shape: 'smooth', style: { stroke: color, lineWidth: 2 } }}
          axis={{
            // 不强制隐藏标签：14 个点在窄列里挤不下时交给 G2 自动抽稀，
            // 但必须禁用旋转，否则日期会被竖排
            x: { labelAutoRotate: false, tick: false, line: true, labelFontSize: 12 },
            y: { tick: false, line: true, grid: true, gridStroke: '#e2e8f0', labelFontSize: 12, gridStrokeOpacity: 1 },
          }}
          tooltip={{ title: 'date', items: [{ channel: 'y', name: valueName ?? t('dataOperations.column.count') }] }}
        />
      )}
    </div>
  );
}
