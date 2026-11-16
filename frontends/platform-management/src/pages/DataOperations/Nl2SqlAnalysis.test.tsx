// @vitest-environment jsdom
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import Nl2SqlAnalysis from './Nl2SqlAnalysis';
import {
  getNl2SqlDetail,
  getNl2SqlOverview,
  listKbOptions,
  listNl2SqlFailures,
  listNl2SqlSlow,
} from '@/api/dataOperations';
import type { DoNl2SqlDetail, DoNl2SqlList, DoNl2SqlOverview, DoNl2SqlRow } from './types';

// 参数插值也带进 key：慢阈值下拉的四个 option 若都返回同一个 key，
// 测试就无法区分「1 秒 / 3 秒 / 5 秒 / 10 秒」，只能靠 title 盲点。
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, params?: Record<string, unknown>) => (params ? `${key}:${JSON.stringify(params)}` : key),
  }),
}));

const spaceState = vi.hoisted(() => ({ current: null as string | null }));
vi.mock('@/hooks/useCurrentSpace', () => ({
  default: () => spaceState.current,
}));

vi.mock('@/api/dataOperations', () => ({
  getNl2SqlOverview: vi.fn(),
  listKbOptions: vi.fn(),
  listNl2SqlSlow: vi.fn(),
  listNl2SqlFailures: vi.fn(),
  getNl2SqlDetail: vi.fn(),
}));

// 区间选择器与图表都依赖真实布局/Canvas，页壳测试只验证「卡」与序列装配，
// 用轻量替身替换掉；趋势图本身的行为不在这里断言。
vi.mock('./components/WindowRangePicker', () => ({
  default: () => <div data-testid="window-picker" />,
}));

vi.mock('./components/MultiTrendChart', () => ({
  default: ({ series }: { series: { name: string }[] }) => (
    <div data-testid="multi-trend">{series.map((item) => item.name).join('|')}</div>
  ),
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
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  spaceState.current = null;
});

const filledOverview: DoNl2SqlOverview = {
  window: { start: '2026-10-31', end: '2026-11-06' },
  totals: { requests: 1234, success: 1200, failed: 34, success_rate: 0.9724, synthetic: 3 },
  latency: {
    sql_p50_ms: 300,
    sql_p95_ms: 1200,
    sql_p99_ms: 3000,
    sql_samples: 120,
    total_p50_ms: 1000,
    total_p95_ms: 4800,
    total_p99_ms: 9000,
    total_samples: 120,
  },
  slow: { threshold_ms: 5000, sql_count: 7, sql_rate: 0.058, total_count: 12, total_rate: 0.1 },
  precompile: { retried: 3, retry_rate: 0.0024, by_code: [{ code: 'unknown_column', count: 2 }] },
  trend: [
    {
      date: '2026-10-31',
      requests: 700,
      failed: 20,
      sql_p95_ms: 1200,
      sql_samples: 80,
      total_p95_ms: 4800,
      total_samples: 80,
    },
    {
      date: '2026-11-01',
      requests: 534,
      failed: 14,
      sql_p95_ms: 900,
      sql_samples: 8,
      total_p95_ms: 3200,
      total_samples: 8,
    },
  ],
  by_source: [
    {
      data_source_id: 'ds_1',
      source_name: 'Source A',
      requests: 1224,
      failed: 30,
      sql_p95_ms: 1100,
      sql_samples: 118,
      slow_count: 7,
    },
    {
      data_source_id: '__unassigned__',
      source_name: '',
      requests: 10,
      failed: 4,
      sql_p95_ms: null,
      sql_samples: 0,
      slow_count: 0,
    },
  ],
  reason_codes: [{ reason_code: 'exec_failed', count: 20 }],
  top_slow: [],
  top_failures: [],
};

const emptyOverview: DoNl2SqlOverview = {
  ...filledOverview,
  totals: { requests: 0, success: 0, failed: 0, success_rate: null, synthetic: 0 },
  latency: {
    sql_p50_ms: null,
    sql_p95_ms: null,
    sql_p99_ms: null,
    sql_samples: 0,
    total_p50_ms: null,
    total_p95_ms: null,
    total_p99_ms: null,
    total_samples: 0,
  },
  slow: { threshold_ms: 5000, sql_count: 0, sql_rate: 0, total_count: 0, total_rate: 0 },
  precompile: { retried: 0, retry_rate: 0, by_code: [] },
  // 后端按天补零，空窗口不是 []，而是 7 个零值点 —— 判空必须看 totals.requests
  trend: Array.from({ length: 7 }, (_, index) => ({
    date: `2026-11-0${index + 1}`,
    requests: 0,
    failed: 0,
    sql_p95_ms: null,
    sql_samples: 0,
    total_p95_ms: null,
    total_samples: 0,
  })),
  by_source: [],
  reason_codes: [],
};

const slowRow: DoNl2SqlRow = {
  id: 'obs_1',
  created_at: '2026-11-05T10:00:00',
  knowledge_base_id: 'kb_1',
  kb_name: 'KB One',
  data_source_id: 'ds_1',
  source_name: 'Source A',
  entity_name: null,
  question: '查询近 7 天销售额',
  sql: 'SELECT 1',
  elapsed_ms: 1500,
  total_ms: 2600,
  row_count: 42,
  outcome: 'success',
  reason_code: null,
  precompile_violation: null,
  precompile_retried: true,
  synthetic: false,
  reason: null,
};

const failureRow: DoNl2SqlRow = {
  id: 'obs_2',
  created_at: '2026-11-05T11:00:00',
  knowledge_base_id: 'kb_1',
  kb_name: 'KB One',
  data_source_id: '__unassigned__',
  source_name: '',
  entity_name: null,
  question: '昨天北京天气',
  sql: null,
  elapsed_ms: null,
  total_ms: 1200,
  row_count: null,
  outcome: 'failed',
  reason_code: 'exec_failed',
  precompile_violation: null,
  precompile_retried: false,
  synthetic: false,
  reason: 'SQL execution failed',
};

const emptyPage: DoNl2SqlList = { items: [], total: 0, page: 1, page_size: 20 };

const slowDetail: DoNl2SqlDetail = {
  ...slowRow,
  rejected_sql: null,
  scene: {
    precompile_violation: 'unknown_column',
    near_miss: [{ entity_name: 'orders', data_source_id: 'ds_1', score: 7 }],
    trajectory: [{ tool: 'schema_search', flags: ['ok'] }],
  },
};

beforeEach(() => {
  spaceState.current = null;
  vi.mocked(listKbOptions).mockResolvedValue([{ id: 'kb_1', name: 'KB One' }]);
  vi.mocked(getNl2SqlOverview).mockResolvedValue(filledOverview);
  vi.mocked(listNl2SqlSlow).mockResolvedValue(emptyPage);
  vi.mocked(listNl2SqlFailures).mockResolvedValue(emptyPage);
  vi.mocked(getNl2SqlDetail).mockResolvedValue(slowDetail);
});

describe('Nl2SqlAnalysis 页壳', () => {
  it('渲染 6 张 KPI、两张趋势卡与两组多序列趋势', async () => {
    render(<Nl2SqlAnalysis />);

    expect(await screen.findByText('dataOperations.nl2sql.kpi.requests')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.kpi.successRate')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.kpi.sqlP95')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.kpi.totalP95')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.kpi.slowQueries')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.kpi.retryRate')).toBeTruthy();

    // 首屏按默认窗口/阈值取 overview
    expect(getNl2SqlOverview).toHaveBeenCalledWith(null, { days: 7 }, {
      kbId: 'all',
      dataSourceId: '',
      slowMs: 5000,
      includeSynthetic: false,
    });

    // KPI 展示：数量、成功率、P95 的格式化
    expect(screen.getByText('1,234')).toBeTruthy();
    expect(screen.getByText('97.24%')).toBeTruthy();
    expect(screen.getByText('1.2 s')).toBeTruthy();
    expect(screen.getByText('4.8 s')).toBeTruthy();

    expect(screen.getByText('dataOperations.nl2sql.section.volumeTrendTitle')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.section.p95TrendTitle')).toBeTruthy();

    const charts = screen.getAllByTestId('multi-trend');
    expect(charts).toHaveLength(2);
    expect(charts[0].textContent).toContain('dataOperations.nl2sql.trend.requests');
    expect(charts[0].textContent).toContain('dataOperations.nl2sql.trend.failed');
    expect(charts[1].textContent).toContain('dataOperations.nl2sql.trend.sqlP95');
    expect(charts[1].textContent).toContain('dataOperations.nl2sql.trend.totalP95');
  });

  it('窗口内没有观测时两张趋势卡显示空态', async () => {
    vi.mocked(getNl2SqlOverview).mockResolvedValue(emptyOverview);

    render(<Nl2SqlAnalysis />);

    const empties = await screen.findAllByText('dataOperations.nl2sql.empty.noObservations');
    expect(empties).toHaveLength(2);
    // KPI 仍然成卡（数字为 0），不是整页空态
    expect(screen.getByText('dataOperations.nl2sql.kpi.requests')).toBeTruthy();
  });

  it('切换空间时重置知识库/数据源筛选，请求不带旧空间 id', async () => {
    spaceState.current = 'space_a';
    vi.mocked(listKbOptions).mockImplementation(async (space) =>
      space === 'space_b'
        ? [{ id: 'kb_b', name: 'KB B' }]
        : [{ id: 'kb_a', name: 'KB A' }],
    );

    const { rerender } = render(<Nl2SqlAnalysis />);
    expect(await screen.findByText('dataOperations.nl2sql.kpi.requests')).toBeTruthy();
    await waitFor(() => expect(listKbOptions).toHaveBeenCalledWith('space_a'));

    // 在空间 A 选中一个知识库
    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]);
    fireEvent.click(await screen.findByTitle('KB A'));
    await waitFor(() =>
      expect(getNl2SqlOverview).toHaveBeenLastCalledWith('space_a', { days: 7 }, {
        kbId: 'kb_a',
        dataSourceId: '',
        slowMs: 5000,
        includeSynthetic: false,
      }),
    );

    // 再按源过滤
    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]);
    fireEvent.click(await screen.findByTitle('Source A'));
    await waitFor(() =>
      expect(getNl2SqlOverview).toHaveBeenLastCalledWith('space_a', { days: 7 }, {
        kbId: 'kb_a',
        dataSourceId: 'ds_1',
        slowMs: 5000,
        includeSynthetic: false,
      }),
    );

    // 切到空间 B：旧 kb / 源都必须先归零，首帧请求不能带它们
    spaceState.current = 'space_b';
    rerender(<Nl2SqlAnalysis />);
    await waitFor(() =>
      expect(getNl2SqlOverview).toHaveBeenLastCalledWith('space_b', { days: 7 }, {
        kbId: 'all',
        dataSourceId: '',
        slowMs: 5000,
        includeSynthetic: false,
      }),
    );
  });

  it('慢查询表渲染耗时/A1b，点击行加载详情并打开抽屉', async () => {
    vi.mocked(listNl2SqlSlow).mockResolvedValue({ items: [slowRow], total: 1, page: 1, page_size: 20 });

    render(<Nl2SqlAnalysis />);

    // 列表列：SQL 耗时 / 端到端耗时 / 行数 / A1b
    expect(await screen.findByText('查询近 7 天销售额')).toBeTruthy();
    expect(screen.getByText('1.5 s')).toBeTruthy();
    expect(screen.getByText('2.6 s')).toBeTruthy();
    expect(screen.getByText('42')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.a1b.retried')).toBeTruthy();

    fireEvent.click(screen.getByText('查询近 7 天销售额').closest('tr') as HTMLElement);

    await waitFor(() => expect(getNl2SqlDetail).toHaveBeenCalledWith('obs_1', null, 'kb_1'));
    // 抽屉标题与 SQL 现场（SQL 只在抽屉里出现，列表没有）
    expect(await screen.findByText('dataOperations.nl2sql.drawer.title')).toBeTruthy();
    expect(await screen.findByText('SELECT 1')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.drawer.nearMiss')).toBeTruthy();
    expect(screen.getByText('dataOperations.nl2sql.drawer.trajectory')).toBeTruthy();
  });

  it('点击失败码按 reason_code 重新请求失败列表', async () => {
    vi.mocked(getNl2SqlOverview).mockResolvedValue({
      ...filledOverview,
      reason_codes: [{ reason_code: 'exec_failed', count: 20 }],
    });
    vi.mocked(listNl2SqlFailures).mockResolvedValue({ items: [failureRow], total: 1, page: 1, page_size: 20 });

    render(<Nl2SqlAnalysis />);

    // 失败码分布（ProportionList）先出现；明细表此时仍是默认的空页
    const labels = await screen.findAllByText('exec_failed');
    fireEvent.click(labels[0]);

    await waitFor(() =>
      expect(listNl2SqlFailures).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        reasonCode: 'exec_failed',
        page: 1,
      })),
    );
  });

  it('数据源分布渲染问答量/失败率/P95/慢查询数，点击行按该源重取两块列表', async () => {
    render(<Nl2SqlAnalysis />);

    expect(await screen.findByText('dataOperations.nl2sql.section.sourceTitle')).toBeTruthy();
    const sourceRow = (await screen.findByText('Source A')).closest('tr') as HTMLElement;
    // 源行的四个数字都来自 by_source：1224 请求、30/1224=2.45%、1.1s、7 条慢查询
    expect(within(sourceRow).getByText('1,224')).toBeTruthy();
    expect(within(sourceRow).getByText('2.45%')).toBeTruthy();
    expect(within(sourceRow).getByText('1.1 s')).toBeTruthy();
    expect(within(sourceRow).getByText('7')).toBeTruthy();

    // 未识别源 P95 样本为 0 → 显示样本不足，而不是一个看似精确的数字
    const unassignedRow = screen
      .getByText('dataOperations.nl2sql.filter.unassignedSource')
      .closest('tr') as HTMLElement;
    expect(
      within(unassignedRow).getByText('dataOperations.nl2sql.kpi.insufficientSamples'),
    ).toBeTruthy();

    fireEvent.click(sourceRow);

    // 与筛选条下拉等价：慢查询与失败列表都带上该源
    await waitFor(() =>
      expect(listNl2SqlSlow).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        dataSourceId: 'ds_1',
      })),
    );
    await waitFor(() =>
      expect(listNl2SqlFailures).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        dataSourceId: 'ds_1',
      })),
    );
    // 选中行高亮，用户能看出当前源是点击筛出来的
    await waitFor(() => expect(sourceRow.className).toContain('is-active'));
  });

  it('点击数据源分布的未识别源行按 __unassigned__ 重取两块列表', async () => {
    render(<Nl2SqlAnalysis />);

    const unassignedRow = (
      await screen.findByText('dataOperations.nl2sql.filter.unassignedSource')
    ).closest('tr') as HTMLElement;
    fireEvent.click(unassignedRow);

    await waitFor(() =>
      expect(listNl2SqlSlow).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        dataSourceId: '__unassigned__',
      })),
    );
    await waitFor(() =>
      expect(listNl2SqlFailures).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        dataSourceId: '__unassigned__',
      })),
    );
  });

  it('点击「未分类」失败码下发 __unclassified__ 哨兵，而不是清空筛选', async () => {
    vi.mocked(getNl2SqlOverview).mockResolvedValue({
      ...filledOverview,
      reason_codes: [{ reason_code: null, count: 4 }],
    });

    render(<Nl2SqlAnalysis />);

    const item = (await screen.findByText('dataOperations.nl2sql.reasonCode.unclassified'))
      .closest('li') as HTMLElement;
    fireEvent.click(item);

    await waitFor(() =>
      expect(listNl2SqlFailures).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        reasonCode: '__unclassified__',
        page: 1,
      })),
    );
    // 不能退化成 null（= 不按码过滤，返回全部失败的超集）
    const calls = vi.mocked(listNl2SqlFailures).mock.calls;
    expect(calls[calls.length - 1]?.[2]?.reasonCode).not.toBeNull();
    // 未分类桶保持高亮
    await waitFor(() => expect(item.className).toContain('is-active'));
  });

  it('选择未识别源后慢查询与失败列表都带 __unassigned__ 过滤', async () => {
    render(<Nl2SqlAnalysis />);
    await screen.findByText('dataOperations.nl2sql.kpi.requests');

    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]);
    fireEvent.click(await screen.findByTitle('dataOperations.nl2sql.filter.unassignedSource'));

    await waitFor(() =>
      expect(listNl2SqlSlow).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        dataSourceId: '__unassigned__',
      })),
    );
    await waitFor(() =>
      expect(listNl2SqlFailures).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        dataSourceId: '__unassigned__',
      })),
    );
  });

  it('切换慢阈值后以新 slow_ms 重取慢查询列表', async () => {
    render(<Nl2SqlAnalysis />);
    await screen.findByText('dataOperations.nl2sql.kpi.requests');

    // 第 3 个 combobox 是慢阈值（前两个是知识库与数据源）
    fireEvent.mouseDown(screen.getAllByRole('combobox')[2]);
    fireEvent.click(
      await screen.findByTitle('dataOperations.nl2sql.filter.slowOption:{"seconds":1}'),
    );

    await waitFor(() =>
      expect(listNl2SqlSlow).toHaveBeenLastCalledWith(null, { days: 7 }, expect.objectContaining({
        slowMs: 1000,
        page: 1,
      })),
    );
  });
});
