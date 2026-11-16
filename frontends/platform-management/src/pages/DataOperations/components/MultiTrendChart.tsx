import { useState } from 'react';
import { Line } from '@ant-design/plots';
import { Empty, Flex, Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import type { ReactNode } from 'react';

/** 单序列上的一个点。`lowSample` = 该点样本数不足，画虚线并在图例/提示中标注 */
export interface MultiTrendPoint {
  date: string;
  value: number | null;
  lowSample?: boolean;
}

export interface MultiTrendSeries {
  /** React key 与显隐状态用，不展示 */
  key: string;
  /** 已翻译的图例名 */
  name: string;
  color: string;
  points: MultiTrendPoint[];
}

interface Props {
  series: MultiTrendSeries[];
  loading?: boolean;
  height?: number;
  /** 空态文案（调用方传 i18n 后的文本） */
  emptyText?: ReactNode;
  /** y 轴与 tooltip 的数值格式化，如毫秒转 `1.2 s` */
  formatValue?: (value: number) => string;
}

interface Row {
  date: string;
  value: number;
  /** G2 分组用内部 run id（必须每段唯一，否则 G2 会把跨空档的点连成一条线） */
  series: string;
  /** tooltip 展示名（低样本段带「样本不足」后缀） */
  name: string;
  /** 低样本段：虚线 */
  dash?: boolean;
  lowSample?: boolean;
  /** 本序列颜色，供点标记的 fill/stroke 回调使用（不依赖颜色比例尺） */
  color: string;
}

/** 一段连续的、同低样本属性且非空的数据点 */
interface Run {
  points: MultiTrendPoint[];
  low: boolean;
}

/**
 * 按「连续且 lowSample 相同」切段。空值本身就是断点。
 *
 * G2 的 line mark 会在同一 series 内跨过缺失的 x 类别直接连线；只把低样本点
 * 从主序列里删掉仍会留下「实线桥」。因此每一段都必须是独立 series：
 * 非低样本段各自一条实线，低样本段各自一条虚线，互不相连。
 */
function toRuns(points: MultiTrendPoint[]): Run[] {
  const runs: Run[] = [];
  let current: MultiTrendPoint[] = [];
  let currentLow = false;

  for (const point of points) {
    if (point.value == null) {
      if (current.length) runs.push({ points: current, low: currentLow });
      current = [];
      continue;
    }
    const low = Boolean(point.lowSample);
    if (current.length && low !== currentLow) {
      runs.push({ points: current, low: currentLow });
      current = [];
    }
    current.push(point);
    currentLow = low;
  }
  if (current.length) runs.push({ points: current, low: currentLow });
  return runs;
}

/** G2 的 style 回调：折线拿到整条序列的抽象数据数组，点标记拿到单个 datum */
const datumOf = (value: unknown): Row | undefined => {
  const item = Array.isArray(value) ? value[0] : value;
  return (item as Row | undefined) ?? undefined;
};

/**
 * 多序列趋势图（折线）。
 *
 * 旧组件 `TrendAreaChart` 只服务单序列，本组件不改它，单独支持：
 * - 多条线共用一个 y 轴，图例可点选显隐（自带图例会把低样本段也列出来，所以自绘）；
 * - 样本不足的点按设计 §5 单独成段：主实线按「非低样本连续段」拆成多条同色实线，
 *   低样本连续段拆成同色虚线；每段都是独立 series，G2 不会跨空档连线。
 *   孤立低样本点没有线段，用空心圆点标出。tooltip 每个日期只命中一条序列，
 *   名字带「（样本不足）」后缀，不会重复两个数值。
 *
 * G2 的线形样式按「序列」生效（style 回调拿到整条序列的抽象数据），
 * 所以虚线必须拆成独立序列，不能在一个序列里按点切换。
 */
export default function MultiTrendChart({ series, loading, height = 260, emptyText, formatValue }: Props) {
  const { t } = useTranslation();
  const [hidden, setHidden] = useState<string[]>([]);
  const lowSuffix = t('dataOperations.nl2sql.trend.lowSample');

  const visible = series.filter((item) => !hidden.includes(item.key));
  const rows: Row[] = visible.flatMap((item) => {
    let solidRun = 0;
    let lowRun = 0;
    return toRuns(item.points).flatMap((run) => {
      const runId = run.low ? `${item.key}__low_${lowRun++}` : `${item.key}__solid_${solidRun++}`;
      const name = run.low ? `${item.name}${lowSuffix}` : item.name;
      return run.points.map((point) => ({
        date: point.date,
        value: point.value as number,
        series: runId,
        name,
        dash: run.low || undefined,
        lowSample: run.low || undefined,
        color: item.color,
      }));
    });
  });

  const toggle = (key: string) =>
    setHidden((prev) => (prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key]));

  // 每段一个唯一 series，颜色仍映射回该指标的原色
  const colorMap = new Map<string, string>();
  rows.forEach((row) => {
    if (!colorMap.has(row.series)) colorMap.set(row.series, row.color);
  });
  const colorDomain = [...colorMap.keys()];
  const colorRange = [...colorMap.values()];

  const legend = (
    <Flex gap={12} wrap="wrap" style={{ marginBottom: 8 }}>
      {series.map((item) => {
        const off = hidden.includes(item.key);
        return (
          <button
            key={item.key}
            type="button"
            aria-pressed={!off}
            onClick={() => toggle(item.key)}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              padding: 0,
              border: 0,
              background: 'transparent',
              cursor: 'pointer',
              fontSize: 12,
              color: off ? '#94a3b8' : '#475569',
              textDecoration: off ? 'line-through' : 'none',
            }}
          >
            <span
              style={{
                width: 14,
                height: 2,
                background: off ? '#cbd5e1' : item.color,
                borderRadius: 1,
              }}
            />
            {item.name}
          </button>
        );
      })}
    </Flex>
  );

  const body = !rows.length ? (
    loading ? (
      <Skeleton active paragraph={{ rows: 5 }} />
    ) : (
      <Flex align="center" justify="center" style={{ height }}>
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyText} />
      </Flex>
    )
  ) : (
    <Line
      data={rows}
      xField="date"
      yField="value"
      colorField="series"
      height={height}
      autoFit
      legend={false}
      scale={{ color: { domain: colorDomain, range: colorRange } }}
      style={{
        lineWidth: 2,
        // style 回调对每条序列调用一次；低样本叠加序列标 dash=true
        lineDash: (datum: unknown) => {
          const list = Array.isArray(datum) ? datum : [datum];
          return list.some((item) => (item as { dash?: boolean } | null)?.dash) ? [4, 4] : undefined;
        },
      }}
      // 低样本点用空心圆（白底 + 同色描边）标出；孤立低样本点没有线段，靠它可见
      point={{
        size: 3,
        shape: 'circle',
        style: {
          fill: (datum: unknown) => (datumOf(datum)?.lowSample ? '#ffffff' : datumOf(datum)?.color),
          stroke: (datum: unknown) => datumOf(datum)?.color,
          lineWidth: (datum: unknown) => (datumOf(datum)?.lowSample ? 2 : 1),
          opacity: 0.9,
        },
      }}
      axis={{
        x: { labelAutoRotate: false, tick: false, line: true, labelFontSize: 12 },
        y: {
          tick: false,
          line: true,
          grid: true,
          gridStroke: '#e2e8f0',
          gridStrokeOpacity: 1,
          labelFontSize: 12,
          ...(formatValue ? { labelFormatter: (value: number) => formatValue(value) } : {}),
        },
      }}
      tooltip={{
        title: 'date',
        items: [
          // 自定义 item 让内部 run id 不出现在 tooltip：每次只显示当前日期的
          // 那一条数据（主实线/低样本段在同一天互斥）
          (datum: unknown) => {
            const row = datumOf(datum);
            return row
              ? {
                  name: row.name,
                  value: formatValue ? formatValue(row.value) : row.value,
                  color: row.color,
                }
              : { name: '', value: '' };
          },
        ],
      }}
    />
  );

  return (
    <div>
      {series.length > 0 && legend}
      {body}
    </div>
  );
}
