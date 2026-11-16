// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import MultiTrendChart, { type MultiTrendSeries } from './MultiTrendChart';

const captured = vi.hoisted(() => ({ props: null as null | Record<string, any> }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

// G2 需要 Canvas；这里只验证整行数据的装配（主实线/低样本虚线分段、点标记样式）
vi.mock('@ant-design/plots', () => ({
  Line: (props: Record<string, any>) => {
    captured.props = props;
    return <div data-testid="line" data-rows={JSON.stringify(props.data)} />;
  },
}));

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

afterEach(() => {
  cleanup();
  captured.props = null;
});

const series: MultiTrendSeries[] = [
  {
    key: 'sqlP95',
    name: 'SQL P95',
    color: '#8b5cf6',
    points: [
      { date: '2026-11-01', value: 1000 },
      { date: '2026-11-02', value: 900, lowSample: true },
      { date: '2026-11-03', value: 800, lowSample: true },
      { date: '2026-11-04', value: 700 },
    ],
  },
];

type TestRow = {
  date: string;
  value: number;
  series: string;
  name: string;
  dash?: boolean;
  lowSample?: boolean;
  color: string;
};

describe('MultiTrendChart', () => {
  it('主实线按非低样本连续段拆分，低样本段单独成虚线序列', () => {
    render(<MultiTrendChart series={series} />);

    const rows: TestRow[] = JSON.parse(screen.getByTestId('line').getAttribute('data-rows') ?? '[]');
    const solid = rows.filter((row) => !row.dash);
    const low = rows.filter((row) => row.dash);

    // high, low, low, high → 非低样本是两段独立的实线（各 1 个点），
    // 不能合成一条跨过低样本日期的线（否则会出现实线桥）
    const solidGroups = new Map<string, TestRow[]>();
    solid.forEach((row) => {
      solidGroups.set(row.series, [...(solidGroups.get(row.series) ?? []), row]);
    });
    expect(solidGroups.size).toBe(2);
    expect([...solidGroups.values()].map((group) => group.length)).toEqual([1, 1]);

    // 连续两个低样本点组成一条虚线序列
    const lowGroups = new Map<string, TestRow[]>();
    low.forEach((row) => {
      lowGroups.set(row.series, [...(lowGroups.get(row.series) ?? []), row]);
    });
    expect(lowGroups.size).toBe(1);
    expect([...lowGroups.values()][0].map((row) => row.date)).toEqual(['2026-11-02', '2026-11-03']);
    expect(low.every((row) => row.name === 'SQL P95dataOperations.nl2sql.trend.lowSample')).toBe(true);
    expect(solid.every((row) => row.name === 'SQL P95')).toBe(true);

    // 每个日期只有一条序列命中 → tooltip 不会重复两个数值
    expect(rows).toHaveLength(4);
    expect(new Set(rows.map((row) => row.date)).size).toBe(4);

    const lineDash = captured.props?.style?.lineDash as (value: unknown) => number[] | undefined;
    expect(lineDash([low[0]])).toEqual([4, 4]);
    expect(lineDash([solid[0]])).toBeUndefined();
  });

  it('tooltip 使用展示名而不是内部 run id', () => {
    render(<MultiTrendChart series={series} />);

    const rows: TestRow[] = JSON.parse(screen.getByTestId('line').getAttribute('data-rows') ?? '[]');
    const solid = rows.find((row) => !row.dash)!;
    const low = rows.find((row) => row.dash)!;
    const tooltipItem = captured.props?.tooltip?.items?.[0] as (datum: unknown) => {
      name: string;
      value: unknown;
      color: string;
    };

    expect(tooltipItem(low)).toEqual({
      name: 'SQL P95dataOperations.nl2sql.trend.lowSample',
      value: 900,
      color: '#8b5cf6',
    });
    expect(tooltipItem(solid)).toEqual({ name: 'SQL P95', value: 1000, color: '#8b5cf6' });
  });

  it('低样本点用空心圆（白底同色描边）标出', () => {
    render(<MultiTrendChart series={series} />);

    const style = captured.props?.point?.style as Record<string, (value: unknown) => unknown>;
    const lowRow = { lowSample: true, color: '#8b5cf6' };
    const normalRow = { color: '#8b5cf6' };

    expect(style.fill(lowRow)).toBe('#ffffff');
    expect(style.stroke(lowRow)).toBe('#8b5cf6');
    expect(style.lineWidth(lowRow)).toBe(2);

    expect(style.fill(normalRow)).toBe('#8b5cf6');
    expect(style.lineWidth(normalRow)).toBe(1);
  });
});
