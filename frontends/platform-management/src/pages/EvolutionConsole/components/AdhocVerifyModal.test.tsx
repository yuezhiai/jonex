// @vitest-environment jsdom
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import AdhocVerifyModal from './AdhocVerifyModal';
import type { AdhocCase, AdhocEvidence } from '../../../api/evolution';
import { fetchAdhocEvidence } from '../../../api/evolution';

vi.mock('react-i18next', () => ({
  // 透传 key；带参数时把参数也拼进去（文案断言才能看到 {{msg}} 的实参）。
  useTranslation: () => ({
    t: (key: string, params?: Record<string, unknown>) =>
      params ? `${key}:${JSON.stringify(params)}` : key,
  }),
}));

vi.mock('../../../api/evolution', () => ({
  fetchAdhocEvidence: vi.fn(),
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
});

const cases: AdhocCase[] = [
  { case_id: 'c1', question: '问题一' },
  { case_id: 'c2', question: '问题二' },
];

const evidence: AdhocEvidence = {
  case_id: 'c1',
  question: '问题一',
  alias: 'job_count',
  concept: '职位数量',
  concept_aliases: ['岗位数', '职位数'],
  columns: ['city', 'n'],
  rows: [['深圳', 9]],
  row_count: 1,
  rows_hash: 'hash-1',
  ok: true,
  error: null,
  evidence_id: 'ev-1',
};

const renderModal = (
  overrides: Partial<React.ComponentProps<typeof AdhocVerifyModal>> = {},
) => render(
  <AdhocVerifyModal
    open
    runId="run-1"
    cases={cases}
    picks={[]}
    onCancel={() => {}}
    onSubmit={() => {}}
    {...overrides}
  />,
);

const viewButtons = () =>
  screen.getAllByRole('button', { name: 'evolutionConsole.adhocVerifyViewData' });
const passRadio = (index = 0) =>
  screen.getAllByRole('radio', { name: 'evolutionConsole.adhocVerifyPass' })[index] as HTMLInputElement;
const failRadio = (index = 0) =>
  screen.getAllByRole('radio', { name: 'evolutionConsole.adhocVerifyFail' })[index] as HTMLInputElement;
const submitButton = () =>
  screen.getByRole('button', { name: 'evolutionConsole.adhocVerifySubmit' }) as HTMLButtonElement;

describe('AdhocVerifyModal', () => {
  beforeEach(() => {
    vi.mocked(fetchAdhocEvidence).mockResolvedValue(evidence);
  });

  it('通过按钮在获取数据前禁用', () => {
    renderModal({ cases: [cases[0]] });

    expect(passRadio().disabled).toBe(true);
    expect(failRadio().disabled).toBe(false);
    expect(submitButton().disabled).toBe(true);
  });

  it('点击查看数据后渲染表格与全名', async () => {
    renderModal({ cases: [cases[0]] });

    fireEvent.click(viewButtons()[0]);

    expect(fetchAdhocEvidence).toHaveBeenCalledWith('run-1', 'c1', []);
    await screen.findByText('职位数量');
    expect(screen.getByText('job_count')).toBeTruthy();
    expect(screen.getByText('岗位数')).toBeTruthy();
    expect(screen.getByText('city')).toBeTruthy();
    expect(screen.getByText('深圳')).toBeTruthy();
    expect(screen.getByText(/adhocVerifyRowCount/)).toBeTruthy();
    expect(passRadio().disabled).toBe(false);
  });

  it('获取失败时可重试且不能判定', async () => {
    vi.mocked(fetchAdhocEvidence)
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(evidence);
    renderModal({ cases: [cases[0]] });

    fireEvent.click(viewButtons()[0]);
    await screen.findByText(/network down/);
    // 请求失败态只能重试：两个判定入口都禁用，理由输入也不出现。
    expect(passRadio().disabled).toBe(true);
    expect(failRadio().disabled).toBe(true);
    expect(submitButton().disabled).toBe(true);
    expect(screen.queryByRole('textbox')).toBeNull();

    // 重试复用同一个「查看数据」按钮
    expect(viewButtons()).toHaveLength(1);
    fireEvent.click(viewButtons()[0]);
    await screen.findByText('深圳');
    expect(fetchAdhocEvidence).toHaveBeenCalledTimes(2);
    expect(passRadio().disabled).toBe(false);
    expect(failRadio().disabled).toBe(false);
  });

  it('payload 没有 evidence_id（执行异常/恢复失败）时不能判定', async () => {
    vi.mocked(fetchAdhocEvidence).mockResolvedValue({
      ...evidence, ok: false, error: '执行失败', evidence_id: null,
    });
    renderModal({ cases: [cases[0]] });

    fireEvent.click(viewButtons()[0]);
    await screen.findByText('执行失败');

    expect(passRadio().disabled).toBe(true);
    expect(failRadio().disabled).toBe(true);
    expect(submitButton().disabled).toBe(true);
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('空结果/拒答仍带票据 → 可以判定', async () => {
    vi.mocked(fetchAdhocEvidence).mockResolvedValue({
      ...evidence,
      columns: [],
      rows: [],
      row_count: 0,
      ok: false,
      error: '查询没有返回数据',
      evidence_id: 'ev-empty',
    });
    renderModal({ cases: [cases[0]] });

    fireEvent.click(viewButtons()[0]);
    await screen.findByText('查询没有返回数据');

    expect(passRadio().disabled).toBe(false);
    expect(failRadio().disabled).toBe(false);
  });

  it('picks 变化后旧请求的响应不回填', async () => {
    let resolveFirst: (value: AdhocEvidence) => void = () => {};
    const first = new Promise<AdhocEvidence>((resolve) => {
      resolveFirst = resolve;
    });
    vi.mocked(fetchAdhocEvidence).mockReturnValueOnce(first);

    const { rerender } = renderModal({ cases: [cases[0]] });
    fireEvent.click(viewButtons()[0]);

    // 勾选变化 = 票据失效：清空旧证据，旧请求即便回来也不能落状态。
    rerender(
      <AdhocVerifyModal
        open
        runId="run-1"
        cases={[cases[0]]}
        picks={['entry-1']}
        onCancel={() => {}}
        onSubmit={() => {}}
      />,
    );
    await act(async () => {
      resolveFirst(evidence);
      await Promise.resolve();
    });

    expect(screen.queryByText('深圳')).toBeNull();
    expect(passRadio().disabled).toBe(true);
  });

  it('全部 case 判定后才能提交', () => {
    const onSubmit = vi.fn();
    renderModal({ onSubmit });

    expect(submitButton().disabled).toBe(true);

    fireEvent.click(failRadio(0));
    expect(submitButton().disabled).toBe(true);

    fireEvent.change(screen.getAllByRole('textbox')[0], {
      target: { value: '数据不对' },
    });
    expect(submitButton().disabled).toBe(true);

    fireEvent.click(failRadio(1));
    expect(submitButton().disabled).toBe(false);

    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenCalledWith([
      { case_id: 'c1', verdict: 'fail', note: '数据不对' },
      { case_id: 'c2', verdict: 'fail' },
    ]);
  });

  it('通过判定带上 evidence_id，不通过判定不带', async () => {
    const onSubmit = vi.fn();
    renderModal({ cases: [cases[0]], onSubmit });

    fireEvent.click(viewButtons()[0]);
    await screen.findByText('深圳');
    fireEvent.click(passRadio());
    fireEvent.click(submitButton());

    expect(onSubmit).toHaveBeenCalledWith([
      {
        case_id: 'c1', verdict: 'pass', evidence_id: 'ev-1',
        row_count: 1, rows_hash: 'hash-1',
      },
    ]);
  });

  it('synonym 验真卡显示全部词条，且能判通过', async () => {
    // 后端 synonym 证据带 `term` / `terms` / `canonical`，不带 alias/concept；
    // 即便两者都在，term/terms/canonical 也是权威（这是值同义词，不是概念别名）。
    // terms[0] 就是 canonical 时，卡片仍要显示全部词条，不能只显示 term
    // （否则会渲染成「标准值 → 标准值」，把有用的别名藏起来）。
    vi.mocked(fetchAdhocEvidence).mockResolvedValue({
      ...evidence,
      alias: 'fallback_alias',
      concept: 'fallback_concept',
      term: '华东环保',
      terms: ['华东环保集团', '华东环保'],
      canonical: '华东环保集团',
    });
    const onSubmit = vi.fn();
    renderModal({ cases: [cases[0]], onSubmit });

    fireEvent.click(viewButtons()[0]);
    await screen.findByText('华东环保集团 / 华东环保');

    // 两个词条都在（joined 文本 + canonical 目标）
    expect(screen.getByText('华东环保集团 / 华东环保')).toBeTruthy();
    expect(screen.getByText('华东环保集团')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.adhocVerifyTerm')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.adhocVerifyCanonical')).toBeTruthy();
    // 有 term/canonical 时不再渲染 alias / concept 文案
    expect(screen.queryByText('evolutionConsole.adhocVerifyAlias')).toBeNull();
    expect(screen.queryByText('evolutionConsole.adhocVerifyConcept')).toBeNull();
    expect(screen.queryByText('fallback_alias')).toBeNull();
    expect(screen.queryByText('fallback_concept')).toBeNull();

    fireEvent.click(passRadio());
    fireEvent.click(submitButton());

    expect(onSubmit).toHaveBeenCalledWith([
      {
        case_id: 'c1', verdict: 'pass', evidence_id: 'ev-1',
        row_count: 1, rows_hash: 'hash-1',
      },
    ]);
  });

  it('synonym 证据没有 terms 时回退显示 term', async () => {
    // 旧后端只回 `term`（没有 `terms`），卡片仍要能显示，不能整块空白。
    vi.mocked(fetchAdhocEvidence).mockResolvedValue({
      ...evidence,
      term: '销量',
      terms: undefined,
      canonical: 'sales',
    });
    renderModal({ cases: [cases[0]] });

    fireEvent.click(viewButtons()[0]);
    await screen.findByText('销量');

    expect(screen.getByText('sales')).toBeTruthy();
  });
});
