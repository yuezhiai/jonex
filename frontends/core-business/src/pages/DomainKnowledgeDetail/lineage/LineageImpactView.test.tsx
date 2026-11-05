import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import LineageImpactView from './LineageImpactView';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}:${JSON.stringify(params)}` : key,
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));

const getLineageImpact = vi.fn();
vi.mock('@/api/lineageImpact', () => ({
  getLineageImpact: (...args: unknown[]) => getLineageImpact(...args),
}));

const TABLES = ['dbo.orders', 'dbo.jobs'];
const COLUMNS = { 'dbo.orders': ['id'], 'dbo.jobs': ['id'] };

const analyzeText = 'domainKnowledge.lineage.impact.analyze';

function edge(overrides: Record<string, unknown> = {}) {
  return {
    from_table: 'dbo.orders', from_column: 'id',
    to_table: 'dbo.jobs', to_column: 'id',
    origin: 'platform', confidence: 0.6, reversed: false,
    ...overrides,
  };
}

function result(overrides: Record<string, unknown> = {}) {
  return {
    anchor: { table: 'dbo.orders', column: 'id' },
    direction: 'downstream', hops: 3, total_edges: 1, truncated: false, unknown: false,
    affected: { tables: ['dbo.jobs'], columns: ['dbo.jobs.id'] },
    levels: [{ hop: 1, nodes: [{ table: 'dbo.jobs', column: 'id' }], edges: [edge()] }],
    ...overrides,
  };
}

function renderView() {
  return render(
    <LineageImpactView kbId="kb1" dsId="ds1" tables={TABLES} columnsByTable={COLUMNS} />,
  );
}

describe('LineageImpactView', () => {
  // 括号体：mockReset() 返回 spy 本身，箭头简写会让 Vitest 把它当 beforeEach
  // 的清理回调再次调用（mockRejectedValue 时变成未处理拒绝，测试假失败）。
  beforeEach(() => { getLineageImpact.mockReset(); });
  afterEach(() => { cleanup(); });

  it('无血缘边时显示显式空态', () => {
    render(<LineageImpactView kbId="kb1" dsId="ds1" tables={[]} columnsByTable={{}} />);
    expect(screen.getByText('domainKnowledge.lineage.impact.noTables')).toBeTruthy();
  });

  it('未点击分析时显示 idle 文案', () => {
    renderView();
    expect(screen.getByText('domainKnowledge.lineage.impact.idle')).toBeTruthy();
    expect(getLineageImpact).not.toHaveBeenCalled();
  });

  it('成功后按跳渲染节点与边', async () => {
    getLineageImpact.mockResolvedValue(result());
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    // 等结果文本，而不是只等 mock 被调用。
    await waitFor(() => expect(screen.getByText('dbo.jobs.id')).toBeTruthy());
    expect(screen.getByText(/dbo\.orders\.id → dbo\.jobs\.id/)).toBeTruthy();
    // 来源档人话化：不显示原始英文。
    expect(screen.getByText('domainKnowledge.lineage.originPlatform')).toBeTruthy();
    expect(screen.queryByText('platform')).toBeNull();
  });

  it('已有成功结果后切换方向：结果区回到 idle（旧结果不显示）', async () => {
    getLineageImpact.mockResolvedValue(result());
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() => expect(screen.getByText('dbo.jobs.id')).toBeTruthy());

    // 方向是第 3 个 combobox（锚点表 / 锚点列 / 方向 / 跳数 / 来源档）。
    fireEvent.mouseDown(screen.getAllByRole('combobox')[2]!);
    const option = await waitFor(() => {
      const found = [
        ...document.querySelectorAll<HTMLElement>('.ant-select-item-option'),
      ].find(
        (el) => el.getAttribute('title') === 'domainKnowledge.lineage.impact.upstream',
      );
      expect(found).toBeTruthy();
      return found as HTMLElement;
    });
    fireEvent.click(option);

    expect(screen.queryByText('dbo.jobs.id')).toBeNull();
    expect(screen.getByText('domainKnowledge.lineage.impact.idle')).toBeTruthy();
  });

  it('已有成功结果后修改锚点列：结果区回到 idle（旧结果不显示）', async () => {
    getLineageImpact.mockResolvedValue(result());
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() => expect(screen.getByText('dbo.jobs.id')).toBeTruthy());

    // 锚点列是第 2 个 combobox（锚点表 / 锚点列 / 方向 / 跳数 / 来源档）。
    fireEvent.change(screen.getAllByRole('combobox')[1]!, { target: { value: 'id' } });

    expect(screen.queryByText('dbo.jobs.id')).toBeNull();
    expect(screen.getByText('domainKnowledge.lineage.impact.idle')).toBeTruthy();
  });

  it('truncated=true 显示告警', async () => {
    getLineageImpact.mockResolvedValue(result({ truncated: true }));
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() =>
      expect(screen.getByText('domainKnowledge.lineage.impact.truncated')).toBeTruthy());
  });

  it('reversed=true 渲染 ←，false 渲染 →', async () => {
    getLineageImpact.mockResolvedValue(result({
      total_edges: 2,
      levels: [{
        hop: 1,
        nodes: [{ table: 'dbo.jobs', column: 'id' }, { table: 'dbo.audit', column: 'id' }],
        edges: [
          edge({ reversed: true }),
          edge({
            from_table: 'dbo.orders', from_column: 'id',
            to_table: 'dbo.audit', to_column: 'id',
            reversed: false,
          }),
        ],
      }],
    }));
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() =>
      expect(screen.getByText(/dbo\.orders\.id ← dbo\.jobs\.id/)).toBeTruthy());
    expect(screen.getByText(/dbo\.orders\.id → dbo\.audit\.id/)).toBeTruthy();
  });

  it('levels=[] 且 unknown=false 显示空结果文案', async () => {
    getLineageImpact.mockResolvedValue(result({
      total_edges: 0,
      affected: { tables: [], columns: [] },
      levels: [],
    }));
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() =>
      expect(screen.getByText('domainKnowledge.lineage.impact.empty')).toBeTruthy());
  });

  it('unknown 显示告警而不是空态', async () => {
    getLineageImpact.mockResolvedValue(result({
      total_edges: 0, unknown: true,
      affected: { tables: [], columns: [] }, levels: [],
    }));
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() =>
      expect(screen.getByText('domainKnowledge.lineage.impact.unknown')).toBeTruthy());
    expect(screen.queryByText('domainKnowledge.lineage.impact.empty')).toBeNull();
  });

  it('请求失败原样展示后端 message', async () => {
    getLineageImpact.mockRejectedValue(new Error('该数据源没有可用的编译 schema'));
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() =>
      expect(screen.getByText('该数据源没有可用的编译 schema')).toBeTruthy());
  });

  it('点击分析按默认锚点下发，column 空白不下发', async () => {
    getLineageImpact.mockResolvedValue(result());
    renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() => expect(getLineageImpact).toHaveBeenCalledWith(
      'kb1',
      'ds1',
      {
        table: 'dbo.orders',
        column: undefined,
        direction: 'downstream',
        hops: 3,
        min_origin: undefined,
      },
    ));
  });

  it('tables 变化触发自动切表时丢弃旧锚点的在飞结果', async () => {
    let resolveOld: (value: unknown) => void = () => {};
    getLineageImpact.mockReturnValue(new Promise((resolve) => { resolveOld = resolve; }));
    const { rerender } = render(
      <LineageImpactView kbId="kb1" dsId="ds1" tables={['dbo.orders', 'dbo.jobs']} columnsByTable={COLUMNS} />,
    );
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() => expect(getLineageImpact).toHaveBeenCalledTimes(1));

    // 当前锚点 dbo.orders 被移出 tables → 自动切到 dbo.jobs，旧请求作废。
    rerender(
      <LineageImpactView kbId="kb1" dsId="ds1" tables={['dbo.jobs']} columnsByTable={COLUMNS} />,
    );
    await waitFor(() => {
      expect(document.querySelector('.ant-select-content')?.textContent).toBe('dbo.jobs');
    });
    // 自动切表同时清 loading：回到 idle，而不是一直转圈。
    expect(screen.getByText('domainKnowledge.lineage.impact.idle')).toBeTruthy();

    await act(async () => {
      resolveOld(result({
        levels: [{
          hop: 1,
          nodes: [{ table: 'dbo.old_result', column: 'id' }],
          edges: [edge({
            from_table: 'dbo.orders', from_column: 'id',
            to_table: 'dbo.old_result', to_column: 'id',
          })],
        }],
      }));
    });

    expect(screen.queryByText(/dbo\.old_result/)).toBeNull();
    expect(screen.getByText('domainKnowledge.lineage.impact.idle')).toBeTruthy();
  });

  it('自动切表后旧锚点已完成的结果也被清空', async () => {
    getLineageImpact.mockResolvedValue(result());
    const { rerender } = render(
      <LineageImpactView kbId="kb1" dsId="ds1" tables={['dbo.orders', 'dbo.jobs']} columnsByTable={COLUMNS} />,
    );
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() => expect(screen.getByText('dbo.jobs.id')).toBeTruthy());

    rerender(
      <LineageImpactView kbId="kb1" dsId="ds1" tables={['dbo.jobs']} columnsByTable={COLUMNS} />,
    );

    await waitFor(() => expect(screen.queryByText('dbo.jobs.id')).toBeNull());
    expect(screen.getByText('domainKnowledge.lineage.impact.idle')).toBeTruthy();
  });

  it('切换数据源后旧请求的写回被丢弃', async () => {
    let resolveOld: (value: unknown) => void = () => {};
    getLineageImpact.mockReturnValue(new Promise((resolve) => { resolveOld = resolve; }));
    const { rerender } = renderView();
    fireEvent.click(screen.getByText(analyzeText));
    await waitFor(() => expect(getLineageImpact).toHaveBeenCalledTimes(1));

    rerender(
      <LineageImpactView
        kbId="kb1"
        dsId="ds2"
        tables={['dbo.customers']}
        columnsByTable={{ 'dbo.customers': ['id'] }}
      />,
    );
    await act(async () => {
      resolveOld(result({
        anchor: { table: 'dbo.orders', column: 'id' },
        levels: [{
          hop: 1,
          nodes: [{ table: 'dbo.old_result', column: 'id' }],
          edges: [edge({
            from_table: 'dbo.orders', from_column: 'id',
            to_table: 'dbo.old_result', to_column: 'id',
          })],
        }],
      }));
    });

    expect(screen.queryByText(/dbo\.old_result/)).toBeNull();
    expect(screen.getByText('domainKnowledge.lineage.impact.idle')).toBeTruthy();
  });
});
