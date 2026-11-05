// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import CorpusTab from './CorpusTab';
import type { EvolutionCorpusCoverageItem } from '../../api/evolution';

// `vi.mock` 工厂会被提升到 import 之前，桩必须用 vi.hoisted 声明（避免 TDZ）。
// t 与 api 桩都放进去：t 必须是稳定引用，否则 `load` 的 useCallback 每次渲染
// 都换身份 → useEffect 自激循环（同 StartRunModal 的纪律）。
const {
  getEvolutionCorpusCoverage, seedEvolutionCorpus, listCorpusCases,
  updateCorpusCase, deleteCorpusCase, t,
} = vi.hoisted(() => ({
  getEvolutionCorpusCoverage: vi.fn(),
  seedEvolutionCorpus: vi.fn(),
  listCorpusCases: vi.fn(),
  updateCorpusCase: vi.fn(),
  deleteCorpusCase: vi.fn(),
  t: (key: string, opts?: { defaultValue?: string }) => opts?.defaultValue ?? key,
}));

vi.mock('../../api/evolution', () => ({
  getEvolutionCorpusCoverage,
  seedEvolutionCorpus,
  listCorpusCases,
  updateCorpusCase,
  deleteCorpusCase,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t }),
}));

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false, media: query, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

beforeAll(() => {
  listCorpusCases.mockResolvedValue({ target: {}, cases: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  listCorpusCases.mockResolvedValue({ target: {}, cases: [] });
});

const row: EvolutionCorpusCoverageItem = {
  kb_id: 'kb1',
  ds_id: 'ds1',
  total: 3,
  enabled: 2,
  by_tier: { single_table: 2 },
  by_expect: { answer: 2 },
  active_version_id: 'cv_1',
  active_hash: 'abcdef123456',
  freshness: 'fresh',
  updated_at: '2026-10-26T10:00:00+08:00',
};

const coverageOf = (items: EvolutionCorpusCoverageItem[]) => ({
  items,
  totals: {
    chains: items.length,
    total: items.reduce((n, r) => n + r.total, 0),
    enabled: items.reduce((n, r) => n + r.enabled, 0),
  },
});

describe('CorpusTab 语料覆盖率与按 Schema 播种', () => {
  it('渲染覆盖率行，播种走该行 kb/ds 且回显结果并刷新', async () => {
    getEvolutionCorpusCoverage.mockResolvedValue(coverageOf([row]));
    seedEvolutionCorpus.mockResolvedValue({
      created: 2, skipped: 0, duplicates: 1, conflicts: 0,
      dry_run: false, mode: 'schema', source: 'seed', items: [], warnings: [],
    });

    render(<CorpusTab />);

    await screen.findByText('kb1');
    expect(screen.getByText('ds1')).toBeTruthy();
    expect(screen.getByText('2/3')).toBeTruthy();
    expect(screen.getByText('single_table=2')).toBeTruthy();
    // 版本 hash 只展示短形式（完整值在 title 里）。
    expect(screen.getByText('abcdef12')).toBeTruthy();
    expect(getEvolutionCorpusCoverage).toHaveBeenCalledTimes(1);

    // 二次确认后才真正调用播种。
    await userEvent.click(
      screen.getByRole('button', { name: 'evolutionConsole.corpusSeedAction' }));
    expect(seedEvolutionCorpus).not.toHaveBeenCalled();
    await userEvent.click(await screen.findByRole('button', { name: 'common.confirm' }));

    await waitFor(() => expect(seedEvolutionCorpus).toHaveBeenCalledWith({
      kb: 'kb1', ds: 'ds1', mode: 'schema',
    }));
    // 结果回显（created=2 等）。
    await screen.findByText('created=2');
    expect(screen.getByText('duplicates=1')).toBeTruthy();
    expect(screen.getByText('conflicts=0')).toBeTruthy();
    // 播种后刷新覆盖率。
    await waitFor(() => expect(getEvolutionCorpusCoverage).toHaveBeenCalledTimes(2));
  });

  it('顶部指定源播种：未填 kb/ds 时禁用，填好后确认调用并回显', async () => {
    // 新源没有语料也没有版本 → 覆盖率表里没有它，只能走顶部入口。
    getEvolutionCorpusCoverage.mockResolvedValue(coverageOf([]));
    seedEvolutionCorpus.mockResolvedValue({
      created: 5, skipped: 0, duplicates: 0, conflicts: 0,
      dry_run: false, mode: 'schema', source: 'seed', items: [], warnings: [],
    });

    render(<CorpusTab />);

    await screen.findByText('evolutionConsole.corpusEmpty');
    const button = screen.getByRole('button', {
      name: 'evolutionConsole.corpusSeedManualAction',
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    // 只填一个字段仍不可点。
    await userEvent.type(
      screen.getByLabelText('evolutionConsole.corpusSeedKbPlaceholder'), 'kb_new');
    expect(button.disabled).toBe(true);

    await userEvent.type(
      screen.getByLabelText('evolutionConsole.corpusSeedDsPlaceholder'), 'ds_new');
    expect(button.disabled).toBe(false);

    // 二次确认后才真正调用，body 显式带 kb/ds/mode。
    await userEvent.click(button);
    expect(seedEvolutionCorpus).not.toHaveBeenCalled();
    await userEvent.click(await screen.findByRole('button', { name: 'common.confirm' }));

    await waitFor(() => expect(seedEvolutionCorpus).toHaveBeenCalledWith({
      kb: 'kb_new', ds: 'ds_new', mode: 'schema',
    }));
    // 结果回显复用同一块 Alert（含链坐标）。
    await screen.findByText('created=5');
    expect(screen.getByText('kb_new/ds_new')).toBeTruthy();
    // 成功后刷新覆盖率。
    await waitFor(() => expect(getEvolutionCorpusCoverage).toHaveBeenCalledTimes(2));
  });

  it('无语料链时显示空态', async () => {
    getEvolutionCorpusCoverage.mockResolvedValue(coverageOf([]));

    render(<CorpusTab />);

    expect(await screen.findByText('evolutionConsole.corpusEmpty')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByRole('button', { name: 'evolutionConsole.corpusSeedAction' })).toBeNull();
  });

  it('加载失败显示错误与 Retry，重试成功后渲染行', async () => {
    getEvolutionCorpusCoverage
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(coverageOf([row]));

    render(<CorpusTab />);

    expect(await screen.findByText('boom')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'common.retry' }));

    await screen.findByText('kb1');
    expect(getEvolutionCorpusCoverage).toHaveBeenCalledTimes(2);
  });
});
