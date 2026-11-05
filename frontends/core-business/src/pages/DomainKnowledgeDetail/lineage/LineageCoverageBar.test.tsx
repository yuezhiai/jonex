import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import LineageCoverageBar from './LineageCoverageBar';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}:${JSON.stringify(params)}` : key,
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));

const getLineageCoverage = vi.fn();
vi.mock('@/api/lineageImpact', () => ({
  getLineageCoverage: (...args: unknown[]) => getLineageCoverage(...args),
}));

describe('LineageCoverageBar', () => {
  // 括号体：mockReset() 返回 spy 本身，箭头简写会让 Vitest 把它当 beforeEach
  // 的清理回调再次调用（mockRejectedValue 时变成未处理拒绝，测试假失败）。
  beforeEach(() => { getLineageCoverage.mockReset(); });
  afterEach(() => { cleanup(); });

  it('展示覆盖率与来源分布', async () => {
    getLineageCoverage.mockResolvedValue({
      tables: { total: 4, covered: 1, ratio: 0.25 },
      columns: { total: 0, covered: 0, ratio: null },
      by_origin: { manual: 2 }, orphan_edges: 1, total_edges: 2,
      per_table: [], truncated: false, unknown: false,
    });
    render(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() => expect(screen.getByText(/1\/4（25%）/)).toBeTruthy());
    expect(screen.getByText(/0\/0（—）/)).toBeTruthy();
    // 来源档人话化：显示 i18n 键（真实环境为「手工录入」等），不是原始档位。
    expect(screen.getByText('domainKnowledge.lineage.originManual: 2')).toBeTruthy();
  });

  it('来源档人话化：不显示原始 platform 档位', async () => {
    getLineageCoverage.mockResolvedValue({
      tables: { total: 2, covered: 1, ratio: 0.5 },
      columns: { total: 0, covered: 0, ratio: null },
      by_origin: { platform: 4 }, orphan_edges: 0, total_edges: 4,
      per_table: [], truncated: false, unknown: false,
    });
    render(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() =>
      expect(screen.getByText('domainKnowledge.lineage.originPlatform: 4')).toBeTruthy());
    expect(screen.queryByText('platform: 4')).toBeNull();
  });

  it('合成桶 unknown 人话化：显示 common.unknown，不显示原始英文', async () => {
    getLineageCoverage.mockResolvedValue({
      tables: { total: 2, covered: 1, ratio: 0.5 },
      columns: { total: 0, covered: 0, ratio: null },
      by_origin: { unknown: 1 }, orphan_edges: 0, total_edges: 1,
      per_table: [], truncated: false, unknown: false,
    });
    render(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() => expect(screen.getByText('common.unknown: 1')).toBeTruthy());
    expect(screen.queryByText('unknown: 1')).toBeNull();
  });

  it('orphan_edges>0 显示孤立边 Tag', async () => {
    getLineageCoverage.mockResolvedValue({
      tables: { total: 2, covered: 1, ratio: 0.5 },
      columns: { total: 0, covered: 0, ratio: null },
      by_origin: {}, orphan_edges: 2, total_edges: 3,
      per_table: [], truncated: false, unknown: false,
    });
    render(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() => expect(screen.getByText(
      'domainKnowledge.lineage.coverage.orphan:{"count":2}')).toBeTruthy());
  });

  it('truncated=true 显示截断 Tag', async () => {
    getLineageCoverage.mockResolvedValue({
      tables: { total: 2, covered: 1, ratio: 0.5 },
      columns: { total: 0, covered: 0, ratio: null },
      by_origin: {}, orphan_edges: 0, total_edges: 1,
      per_table: [], truncated: true, unknown: false,
    });
    render(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() =>
      expect(screen.getByText('domainKnowledge.lineage.coverage.truncated')).toBeTruthy());
  });

  it('unknown 显式告警', async () => {
    getLineageCoverage.mockResolvedValue({
      tables: { total: 4, covered: 0, ratio: null },
      columns: { total: 0, covered: 0, ratio: null },
      by_origin: {}, orphan_edges: 0, total_edges: 0,
      per_table: [], truncated: false, unknown: true,
    });
    render(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() =>
      expect(screen.getByText('domainKnowledge.lineage.coverage.unknown')).toBeTruthy());
  });

  it('无 schema 错误原样展示 message', async () => {
    getLineageCoverage.mockRejectedValue(new Error('该数据源没有可用的编译 schema：ds1'));
    render(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() =>
      expect(screen.getByText('该数据源没有可用的编译 schema：ds1')).toBeTruthy());
  });

  it('错误态可重试：点「重试」重新请求并恢复展示', async () => {
    getLineageCoverage.mockRejectedValueOnce(new Error('boom'));
    render(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() => expect(screen.getByText('boom')).toBeTruthy());

    getLineageCoverage.mockResolvedValueOnce({
      tables: { total: 2, covered: 1, ratio: 0.5 },
      columns: { total: 0, covered: 0, ratio: null },
      by_origin: {}, orphan_edges: 0, total_edges: 1,
      per_table: [], truncated: false, unknown: false,
    });
    fireEvent.click(screen.getByRole('button', { name: 'common.retry' }));

    await waitFor(() => expect(getLineageCoverage).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText(/1\/2（50%）/)).toBeTruthy());
    expect(screen.queryByText('boom')).toBeNull();
  });

  it('revision 变化触发重查', async () => {
    getLineageCoverage.mockResolvedValue({
      tables: { total: 0, covered: 0, ratio: null },
      columns: { total: 0, covered: 0, ratio: null },
      by_origin: {}, orphan_edges: 0, total_edges: 0,
      per_table: [], truncated: false, unknown: false,
    });
    const { rerender } = render(
      <LineageCoverageBar kbId="kb1" dsId="ds1" revision="r1" />);
    await waitFor(() => expect(getLineageCoverage).toHaveBeenCalledTimes(1));
    rerender(<LineageCoverageBar kbId="kb1" dsId="ds1" revision="r2" />);
    await waitFor(() => expect(getLineageCoverage).toHaveBeenCalledTimes(2));
  });
});
