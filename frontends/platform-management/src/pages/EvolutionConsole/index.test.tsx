// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import EvolutionConsole from './index';
import type { EvolutionStatus } from '../../api/evolution';

// `vi.mock` 工厂会被提升到 import 之前，桩必须用 vi.hoisted 声明（避免 TDZ）。
const { getEvolutionStatus } = vi.hoisted(() => ({
  getEvolutionStatus: vi.fn(),
}));

vi.mock('../../api/evolution', () => ({ getEvolutionStatus }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

// 子 Tab 只验证容器行为：挂载/可见性，不重复测各自的数据链路。
vi.mock('./RunsTab', () => ({
  default: ({ status }: { status: EvolutionStatus | null }) => (
    <div data-testid="runs-tab">{status ? 'with-status' : 'without-status'}</div>
  ),
}));
vi.mock('./FailuresTab', () => ({ default: () => <div>failures-tab</div> }));
vi.mock('./LedgersTab', () => ({ default: () => <div>ledgers-tab</div> }));
vi.mock('./DislikesTab', () => ({ default: () => <div>dislikes-tab</div> }));
vi.mock('./CorpusTab', () => ({ default: () => <div>corpus-tab</div> }));

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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const renderConsole = () => render(
  <MemoryRouter initialEntries={['/evolution?tab=runs']}>
    <EvolutionConsole />
  </MemoryRouter>,
);

describe('EvolutionConsole status degradation', () => {
  it('renders tabs and RunsTab when /status rejects (KB manager 403)', async () => {
    getEvolutionStatus.mockRejectedValue(new Error('403 forbidden'));

    renderConsole();

    expect(await screen.findByText('evolutionConsole.statusUnavailable'))
      .toBeTruthy();
    // 整页错误页不再出现；Tab 列表与运行内容照常渲染（status 为空）。
    expect(screen.queryByText('evolutionConsole.loadFailedTitle')).toBeNull();
    expect(screen.getByTestId('runs-tab').textContent).toBe('without-status');
    expect(screen.getByText('evolutionConsole.tabRuns')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.tabCorpus')).toBeTruthy();
  });

  it('passes status to RunsTab and shows no warning when /status resolves', async () => {
    getEvolutionStatus.mockResolvedValue({
      ok: true, repo_root: '/repo', runs_dir: '/runs',
      active_run: null, note: '',
    } satisfies EvolutionStatus);

    renderConsole();

    await waitFor(() => expect(getEvolutionStatus).toHaveBeenCalled());
    expect(await screen.findByTestId('runs-tab')).toBeTruthy();
    expect(screen.getByTestId('runs-tab').textContent).toBe('with-status');
    expect(screen.queryByText('evolutionConsole.statusUnavailable')).toBeNull();
  });
});
