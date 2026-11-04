import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import ResultBlocksView from './ResultBlocksView';

vi.mock('./resultBlocksSetup', () => ({
  default: { init: () => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }) },
}));
afterEach(cleanup);

const blocks = [
  { type: 'text' as const, text: '销售额：42' },
  { type: 'chart' as const, chart_type: 'bar' as const, x: '月',
    series: [{ name: '销售额', field: '销售额' }] },
  { type: 'table' as const,
    columns: [{ name: '月', type: 'date' }, { name: '销售额', type: 'integer' }],
    rows: [['2026-01', 42]], row_count: 1 },
];

it('渲染 text + chart + table', () => {
  render(<ResultBlocksView blocks={blocks} />);
  expect(screen.getByText('销售额：42')).toBeTruthy();
  expect(screen.getByText('2026-01')).toBeTruthy();
  expect(screen.getByText('42')).toBeTruthy();
});

it('includeText=false 时只画图表', () => {
  render(<ResultBlocksView blocks={blocks} includeText={false} />);
  expect(screen.queryByText('销售额：42')).toBeNull();
  expect(screen.getByText('2026-01')).toBeTruthy();
});
