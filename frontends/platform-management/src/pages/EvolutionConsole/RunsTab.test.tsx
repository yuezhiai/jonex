// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import RunsTab from './RunsTab';
import type { EvolutionRunSummary, EvolutionStatus } from '../../api/evolution';
import zhLocale from '../../locales/zh.json';
import enLocale from '../../locales/en.json';

// `vi.mock` 工厂会被提升到 import 之前，桩必须用 vi.hoisted 声明（避免 TDZ）。
const { listEvolutionRuns, batchAdoptRuns } = vi.hoisted(() => ({
  listEvolutionRuns: vi.fn(),
  batchAdoptRuns: vi.fn(),
}));

vi.mock('../../api/evolution', () => ({
  listEvolutionRuns,
  batchAdoptRuns,
  rollbackRun: vi.fn(),
  listCorpusCases: vi.fn().mockResolvedValue({ target: {}, cases: [] }),
  startEvolutionRun: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: { defaultValue?: string }) => opts?.defaultValue ?? key,
  }),
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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

const POLL_MS = 10_000;

const noActiveRun: EvolutionStatus = {
  ok: true,
  repo_root: '/repo',
  runs_dir: '/runs',
  active_run: null,
  note: '',
};

const runSummary = (
  runId: string,
  status: string,
  overrides: Partial<EvolutionRunSummary> = {},
): EvolutionRunSummary => ({
  run_id: runId,
  kind: null,
  kb: null,
  ds: null,
  stage: 'gated',
  status,
  created_at: null,
  updated_at: null,
  error: '',
  stuck: false,
  stale: false,
  gate: null,
  entries_count: 1,
  target_cases: null,
  bench_status: status === 'adopting' ? 'pending' : null,
  ...overrides,
});

const startButton = async () =>
  (await screen.findByRole('button', { name: 'evolutionConsole.startRun' })) as HTMLButtonElement;

describe('RunsTab adopting 与 Start 按钮', () => {
  it('只有 adopting 时不阻塞 Start，但仍继续轮询', async () => {
    listEvolutionRuns.mockResolvedValue({ runs: [runSummary('run_adopt', 'adopting')] });
    const intervalSpy = vi.spyOn(window, 'setInterval');

    render(<RunsTab status={noActiveRun} onChanged={() => {}} />);

    const button = await startButton();
    // 先等行真正渲染出来，否则 disabled=false 可能只是加载前的默认态（假绿）。
    await screen.findByText('run_adopt');
    // 服务端 has_active 只认 queued|running：adopting 不该让用户点不了发起。
    await waitFor(() => expect(button.disabled).toBe(false));
    // 但仍要轮询推进采纳态，并展示轮询提示。
    await waitFor(() => expect(intervalSpy).toHaveBeenCalledWith(expect.any(Function), POLL_MS));
    expect(screen.getByText('evolutionConsole.pollHint')).toBeTruthy();
  });

  it('queued 也阻塞 Start（与服务端 has_active 同口径），并继续轮询', async () => {
    listEvolutionRuns.mockResolvedValue({ runs: [runSummary('run_queued', 'queued')] });
    const intervalSpy = vi.spyOn(window, 'setInterval');

    render(<RunsTab status={noActiveRun} onChanged={() => {}} />);

    const button = await startButton();
    await screen.findByText('run_queued');
    await waitFor(() => expect(button.disabled).toBe(true));
    await waitFor(() => expect(intervalSpy).toHaveBeenCalledWith(expect.any(Function), POLL_MS));
  });

  it('active_run 残留但轮询列表已是 adopting → 不阻塞发起，仍轮询', async () => {
    listEvolutionRuns.mockResolvedValue({ runs: [runSummary('run_adopt', 'adopting')] });
    const staleActiveRun: EvolutionStatus = {
      ...noActiveRun,
      active_run: { run_id: 'run_adopt', stage: 'adopting' },
    };

    render(<RunsTab status={staleActiveRun} onChanged={() => {}} />);

    const button = await startButton();
    // 初始按 active_run 阻塞；拿到列表后按同 id 的对账结果放行。
    await screen.findByText('run_adopt');
    await waitFor(() => expect(button.disabled).toBe(false));
    expect(screen.getByText('evolutionConsole.pollHint')).toBeTruthy();
  });

  it('active_run 不在轮询列表（可能被截断）→ 保持旧判定，仍阻塞', async () => {
    listEvolutionRuns.mockResolvedValue({ runs: [runSummary('run_other', 'evaluated')] });
    const activeRun: EvolutionStatus = {
      ...noActiveRun,
      active_run: { run_id: 'run_missing', stage: 'running' },
    };

    render(<RunsTab status={activeRun} onChanged={() => {}} />);

    const button = await startButton();
    await screen.findByText('run_other');
    expect(button.disabled).toBe(true);
  });

  it('running 仍阻塞 Start', async () => {
    listEvolutionRuns.mockResolvedValue({ runs: [runSummary('run_busy', 'running')] });

    render(<RunsTab status={noActiveRun} onChanged={() => {}} />);

    const button = await startButton();
    await waitFor(() => expect(button.disabled).toBe(true));
  });

  it('adopting 且 stale 不算活跃：不轮询', async () => {
    listEvolutionRuns.mockResolvedValue({
      runs: [runSummary('run_stale', 'adopting', { stale: true })],
    });
    const intervalSpy = vi.spyOn(window, 'setInterval');

    render(<RunsTab status={noActiveRun} onChanged={() => {}} />);

    const button = await startButton();
    await screen.findByText('run_stale');
    expect(button.disabled).toBe(false);
    expect(intervalSpy).not.toHaveBeenCalledWith(expect.any(Function), POLL_MS);
  });
});

// ── 批量采纳（演化台 P1）：同 KB + 每链一个 run ──

/** 表格行的勾选框（避开表头的全选勾选框）。 */
const rowCheckbox = (runId: string) => {
  const row = screen.getByText(runId).closest('tr') as HTMLElement;
  return within(row).getByRole('checkbox') as HTMLInputElement;
};

/** 禁用原因（antd 把 getCheckboxProps.title 挂在勾选框外层 span 上）。 */
const rowCheckboxTitle = (runId: string) =>
  rowCheckbox(runId).closest('.ant-checkbox')?.getAttribute('title') ?? '';

describe('RunsTab 批量采纳', () => {
  it('disables selecting two runs on the same chain or different KB', async () => {
    listEvolutionRuns.mockResolvedValue({
      runs: [
        // alias / mapping 共用 abstract 链（KB 级）→ run_b 与 run_a 同链。
        runSummary('run_a', 'evaluated', { kind: 'alias', kb: 'kb_1' }),
        runSummary('run_b', 'evaluated', { kind: 'mapping', kb: 'kb_1' }),
        // query_rule 是 compiled 链，按 ds 区分：run_c 与 run_e 不同链。
        runSummary('run_c', 'evaluated', { kind: 'query_rule', kb: 'kb_1', ds: 'ds_1' }),
        runSummary('run_e', 'evaluated', { kind: 'query_rule', kb: 'kb_1', ds: 'ds_2' }),
        // 跨 KB 一律不可同批。
        runSummary('run_d', 'evaluated', { kind: 'alias', kb: 'kb_2' }),
        // 缺 kind 的存量 run 无法推导链键 → 直接禁用。
        runSummary('run_f', 'evaluated', { kind: null, kb: 'kb_1' }),
      ],
    });

    render(<RunsTab status={noActiveRun} onChanged={() => {}} />);
    await screen.findByText('run_a');

    // 未选任何行时，只有「链键无法推导」的 run_f 被禁用。
    expect(rowCheckbox('run_a').disabled).toBe(false);
    expect(rowCheckbox('run_b').disabled).toBe(false);
    expect(rowCheckbox('run_f').disabled).toBe(true);
    expect(rowCheckboxTitle('run_f')).toContain('batchSelectUnavailable');

    fireEvent.click(rowCheckbox('run_a'));

    await waitFor(() => expect(rowCheckbox('run_b').disabled).toBe(true));
    // 同链：可见原因里带上撞链的 run。
    expect(rowCheckboxTitle('run_b')).toContain('batchSelectSameChain');
    // 跨 KB：可见原因里带上已选 KB。
    expect(rowCheckbox('run_d').disabled).toBe(true);
    expect(rowCheckboxTitle('run_d')).toContain('batchSelectOtherKb');
    // 其它链仍可选。
    expect(rowCheckbox('run_c').disabled).toBe(false);
    expect(rowCheckbox('run_e').disabled).toBe(false);

    // 取消选择后约束解除（自己的勾选框必须始终可点）。
    fireEvent.click(rowCheckbox('run_a'));
    await waitFor(() => expect(rowCheckbox('run_b').disabled).toBe(false));
    expect(rowCheckbox('run_d').disabled).toBe(false);
  });

  it('synonym 是独立 KB 级链：同链只能选一条，且不与 alias 撞链', async () => {
    listEvolutionRuns.mockResolvedValue({
      runs: [
        runSummary('run_s1', 'evaluated', { kind: 'synonym', kb: 'kb_1' }),
        runSummary('run_s2', 'evaluated', { kind: 'synonym', kb: 'kb_1' }),
        // alias 走 abstract 链；synonym 不与它互斥（后端 scope_for_kind 同口径）。
        runSummary('run_a', 'evaluated', { kind: 'alias', kb: 'kb_1' }),
      ],
    });

    render(<RunsTab status={noActiveRun} onChanged={() => {}} />);
    await screen.findByText('run_s1');

    expect(rowCheckbox('run_s1').disabled).toBe(false);
    fireEvent.click(rowCheckbox('run_s1'));

    await waitFor(() => expect(rowCheckbox('run_s2').disabled).toBe(true));
    expect(rowCheckboxTitle('run_s2')).toContain('batchSelectSameChain');
    expect(rowCheckbox('run_a').disabled).toBe(false);

    // 链文案必须能解析到翻译（缺 key 时弹窗会渲染原始 key）。
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.batchAdopt' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('evolutionConsole.batchAdoptChainScope.synonym')).toBeTruthy();
    // 且两种语言都真的登记了这个叶子键（mock 的 t 掩盖不了缺翻译）。
    const zhScopeCopy = zhLocale.evolutionConsole.batchAdoptChainScope as Record<string, string>;
    const enScopeCopy = enLocale.evolutionConsole.batchAdoptChainScope as Record<string, string>;
    expect(zhScopeCopy.synonym).toBeTruthy();
    expect(enScopeCopy.synonym).toBeTruthy();
  });

  it('submits batch adopt with selected run ids and shows per-run results', async () => {
    listEvolutionRuns.mockResolvedValue({
      runs: [
        runSummary('run_a', 'evaluated', { kind: 'alias', kb: 'kb_1' }),
        runSummary('run_c', 'evaluated', { kind: 'query_rule', kb: 'kb_1', ds: 'ds_1' }),
        runSummary('run_d', 'evaluated', { kind: 'alias', kb: 'kb_2' }),
      ],
    });
    batchAdoptRuns.mockResolvedValue({
      batch_id: 'batch_x',
      accepted: 1,
      rejected: 1,
      results: [
        { run_id: 'run_a', accepted: true, status: 'adopting', code: null, message: null },
        { run_id: 'run_c', accepted: false, status: null, code: 1001, message: '状态已变' },
      ],
    });

    render(<RunsTab status={noActiveRun} onChanged={() => {}} />);
    await screen.findByText('run_a');

    fireEvent.click(rowCheckbox('run_a'));
    await waitFor(() => expect(rowCheckbox('run_c').disabled).toBe(false));
    fireEvent.click(rowCheckbox('run_c'));

    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.batchAdopt' }));

    const dialog = await screen.findByRole('dialog');
    // 批量即确认：弹窗里必须有明确提示（没有预览、没有 confirm 参数）。
    expect(within(dialog).getByText('evolutionConsole.batchAdoptWarn')).toBeTruthy();
    // 列 run / kb / 链：只列已选的两条（跨 KB 的 run_d 不在内）。
    expect(within(dialog).getAllByText('kb_1').length).toBeGreaterThan(0);
    expect(within(dialog).queryByText('kb_2')).toBeNull();
    expect(within(dialog).getAllByText('run_a').length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText('run_c').length).toBeGreaterThan(0);

    fireEvent.change(
      within(dialog).getByPlaceholderText('evolutionConsole.batchAdoptOverridePlaceholder'),
      { target: { value: '门禁人工放行' } },
    );
    const callsBeforeSubmit = listEvolutionRuns.mock.calls.length;
    fireEvent.click(within(dialog).getByRole('button', { name: 'evolutionConsole.batchAdoptSubmit' }));

    await waitFor(() => expect(batchAdoptRuns).toHaveBeenCalledTimes(1));
    expect(batchAdoptRuns).toHaveBeenCalledWith({
      run_ids: ['run_a', 'run_c'],
      override_reason: '门禁人工放行',
    });

    // 结果逐条展示：成功 / 失败 + 原因。
    await waitFor(() =>
      expect(within(dialog).getAllByText('evolutionConsole.batchAdoptAccepted').length).toBeGreaterThan(0));
    expect(within(dialog).getAllByText('evolutionConsole.batchAdoptRejected').length).toBeGreaterThan(0);
    // 错误码与失败原因同格展示（`[1001] 状态已变`），按子串匹配。
    expect(within(dialog).getByText(/状态已变/)).toBeTruthy();

    // 结果回来后刷新列表（提交后比提交前多一次列表请求）。
    await waitFor(() =>
      expect(listEvolutionRuns.mock.calls.length).toBeGreaterThan(callsBeforeSubmit));
  });
});
