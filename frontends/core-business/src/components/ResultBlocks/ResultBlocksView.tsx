/** W4.6 结果契约渲染：text / chart / table 三种块。
 *
 * 聊天里 `text` 已由外层正文渲染，传 `includeText={false}` 只画图/表；
 * 独立页面（指标问数等）用默认值即可完整渲染。
 */
import { useEffect, useRef } from 'react';
import type {
  ResultBlock, ResultChartBlock, ResultTableBlock, ResultTextBlock,
} from '@/types/resultBlocks';
import echarts from './resultBlocksSetup';

interface Props {
  blocks: ResultBlock[];
  includeText?: boolean;
}

function chartOption(chart: ResultChartBlock, table: ResultTableBlock) {
  const names = table.columns.map((column) => column.name);
  const xIndex = names.indexOf(chart.x);
  const categories = table.rows.map((row) => (xIndex >= 0 ? row[xIndex] : ''));
  return {
    tooltip: { trigger: 'axis' },
    legend: chart.series.length > 1 ? { top: 0 } : undefined,
    grid: { left: 48, right: 16, top: chart.series.length > 1 ? 32 : 16, bottom: 32 },
    xAxis: { type: 'category', data: categories },
    yAxis: { type: 'value' },
    series: chart.series.map((series) => {
      const index = names.indexOf(series.field);
      return {
        name: series.name,
        type: chart.chart_type,
        data: table.rows.map((row) => (index >= 0 ? row[index] : null)),
      };
    }),
  };
}

function ResultChart({ chart, table }: {
  chart: ResultChartBlock;
  table: ResultTableBlock;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const instance = echarts.init(ref.current);
    instance.setOption(chartOption(chart, table) as never);
    const resize = () => instance.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      instance.dispose();
    };
  }, [chart, table]);
  return <div ref={ref} style={{ width: '100%', height: 260 }} />;
}

export default function ResultBlocksView({ blocks, includeText = true }: Props) {
  const text = includeText
    ? blocks.filter((block): block is ResultTextBlock => block.type === 'text') : [];
  const table = blocks.find(
    (block): block is ResultTableBlock => block.type === 'table');
  const chart = blocks.find(
    (block): block is ResultChartBlock => block.type === 'chart');
  return (
    <div>
      {text.map((block, index) => (
        <div key={`t${index}`} style={{ whiteSpace: 'pre-wrap', fontSize: 13, color: '#334155', lineHeight: 1.7 }}>
          {block.text}
        </div>
      ))}
      {chart && table && <ResultChart chart={chart} table={table} />}
      {table && (
        <div style={{ marginTop: chart ? 8 : 0, overflowX: 'auto' }}>
          <table style={{ borderCollapse: 'collapse', fontSize: 12, minWidth: '100%' }}>
            <thead>
              <tr>
                {table.columns.map((column) => (
                  <th key={column.name} style={{
                    border: '1px solid #e2e8f0', padding: '4px 10px',
                    background: '#f8fafc', textAlign: 'left', fontWeight: 600,
                  }}>
                    {column.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.length === 0 && (
                <tr>
                  <td colSpan={table.columns.length}
                      style={{ border: '1px solid #e2e8f0', padding: '4px 10px', color: '#94a3b8' }}>
                    （0 行）
                  </td>
                </tr>
              )}
              {table.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((value, valueIndex) => (
                    <td key={valueIndex} style={{ border: '1px solid #e2e8f0', padding: '4px 10px' }}>
                      {value === null || value === undefined ? '' : String(value)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
