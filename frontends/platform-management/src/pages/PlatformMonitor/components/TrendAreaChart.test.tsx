// @vitest-environment jsdom
/**
 * 趋势图三态判定（2026-10-30 口径变更）。
 *
 * 后端对 5m / hour / day 三档都补网格后，「整窗无数据」会返回一组 `value=null`
 * 的点而不是空数组。展示口径要求：这种情况按 0 展示（贴底基线），不再走「暂无数据」；
 * 只有接口返回空数组（降级 / 未取到）才是真的空态。
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';

vi.mock('@ant-design/plots', () => ({
  Area: (props: { data: unknown[] }) => (
    <div data-testid="trend-area" data-points={props.data.length} />
  ),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import TrendAreaChart from './TrendAreaChart';

const point = (date: string, value: number | null) => ({ date, value });

describe('TrendAreaChart 空态判定', () => {
  it('整窗全为 null（后端补出的网格）→ 画 0 基线，不走空态', () => {
    render(
      <TrendAreaChart
        data={[point('14:00', null), point('15:00', null), point('16:00', null)]}
      />,
    );

    expect(
      screen.getByTestId('trend-area').getAttribute('data-points'),
    ).toBe('3');
    expect(screen.queryByText('platformMonitor.empty.noData')).toBeNull();
  });

  it('部分空桶 + 有数据 → 照常画图', () => {
    render(<TrendAreaChart data={[point('14:00', null), point('15:00', 2)]} />);

    expect(
      screen.getByTestId('trend-area').getAttribute('data-points'),
    ).toBe('2');
  });

  it('空数组（接口降级 / 未取到）→ 空态', () => {
    render(<TrendAreaChart data={[]} />);

    expect(screen.getByText('platformMonitor.empty.noData')).toBeTruthy();
    expect(screen.queryByTestId('trend-area')).toBeNull();
  });

  it('首次取数（loading 且无任何真实值）→ 骨架，不先画 0 基线', () => {
    render(<TrendAreaChart data={[point('14:00', null)]} loading />);

    expect(screen.queryByTestId('trend-area')).toBeNull();
    expect(screen.queryByText('platformMonitor.empty.noData')).toBeNull();
  });

  it('刷新中（loading 但已有真实值）→ 保留上一帧，不闪骨架', () => {
    render(<TrendAreaChart data={[point('14:00', null), point('15:00', 2)]} loading />);

    expect(screen.getByTestId('trend-area')).toBeTruthy();
  });
});
