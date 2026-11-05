// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { message } from 'antd';
import RunDetail from './RunDetail';
import type {
  CandidateAdhocCase, CandidateItem, PatchEntry, RunBundle,
} from '../../api/evolution';

// `vi.mock` 工厂会被提升到 import 之前，桩必须用 vi.hoisted 声明（避免 TDZ）。
const {
  getRunBundle, listVersions, promoteAdhoc, previewAdopt, adjustEntry,
  confirmAdopt, fetchAdhocEvidence,
} = vi.hoisted(() => ({
  getRunBundle: vi.fn(),
  listVersions: vi.fn(),
  promoteAdhoc: vi.fn(),
  previewAdopt: vi.fn(),
  adjustEntry: vi.fn(),
  confirmAdopt: vi.fn(),
  fetchAdhocEvidence: vi.fn(),
}));

vi.mock('../../api/evolution', () => ({
  getRunBundle,
  listVersions,
  promoteAdhoc,
  previewAdopt,
  getRunLog: vi.fn(),
  confirmAdopt,
  fetchAdhocEvidence,
  rejectRun: vi.fn(),
  rollbackRun: vi.fn(),
  adjustEntry,
  editEntry: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ runId: 'run_1' }),
  useNavigate: () => vi.fn(),
}));

// 保留参数供断言（toast 计数就在这里）：`key k=v …`。
// t 必须是**稳定引用**：RunDetail 的 load useCallback 依赖它，每次新建会触发 effect 死循环（OOM）。
vi.mock('react-i18next', () => {
  const t = (key: string, opts?: Record<string, unknown>) => {
    if (!opts) return key;
    const params = Object.entries(opts).map(([k, v]) => `${k}=${String(v)}`).join(' ');
    return params ? `${key} ${params}` : key;
  };
  return { useTranslation: () => ({ t }) };
});

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

// 与真实 bundle 同形：ad-hoc 语料挂在 `candidates[].evidence`（`patch` 是白名单投影，
// 服务端不下发 `candidate`）。fixture 若再放 `patch.candidate` 会掩盖数据源缺陷。
const bundleWithCandidates = (candidates: CandidateItem[]): RunBundle => ({
  run: {
    run_id: 'run_1',
    kind: 'alias',
    kb: null,
    ds: null,
    stage: 'gated',
    status: 'evaluated',
    created_at: null,
    updated_at: null,
    error: '',
    stuck: false,
    stale: false,
    gate: null,
    entries_count: 0,
    target_cases: null,
  },
  stuck: false,
  stale: false,
  report_md: null,
  gate: null,
  patch: {
    patch_id: 'patch_1',
    kind: 'alias',
    kb: 'kb_demo',
    ds: null,
    entries: [],
    skipped: [],
    target_cases: [],
    rollback_added: [],
  },
  eval: { parent: null, candidate: null },
  probe: null,
  smoke: null,
  candidates,
  rounds: [],
  artifacts: [],
});

const bundleWithAdhoc = (adhoc: CandidateAdhocCase[]): RunBundle =>
  bundleWithCandidates([{ candidate_no: 1, evidence: { adhoc_cases: adhoc } }]);

const bundleWithEntries = (kind: string, entries: PatchEntry[]): RunBundle => {
  const bundle = bundleWithCandidates([]);
  return {
    ...bundle,
    run: { ...bundle.run, kind },
    patch: { ...bundle.patch!, kind, entries },
  };
};

const verified = (caseId: string, question: string): CandidateAdhocCase => ({
  case_id: caseId,
  question,
  verification: 'verified',
});

const unverified = (caseId: string, question: string): CandidateAdhocCase => ({
  case_id: caseId,
  question,
  verification: 'unverified',
});

/** 人工验真结论：verified 但没有 SQL truth（`manual_adhoc_outcomes`）。 */
const humanVerified = (caseId: string, question: string): CandidateAdhocCase => ({
  case_id: caseId,
  question,
  verification: 'verified',
  truth: null,
  verified_by: 'human',
});

/** 后端 `created[]` 的真实形状（`{id, name, question, tier, expect, control?}`）。 */
const createdRow = (caseId: string) => ({
  id: `corpus_${caseId}`,
  name: `adhoc_${caseId}`,
  question: `问题 ${caseId}`,
  tier: 'adhoc',
  expect: 'answer',
  control: ['SELECT 1'],
});

const renderBundle = async (bundle: RunBundle) => {
  getRunBundle.mockResolvedValue(bundle);
  listVersions.mockResolvedValue({ items: [] });
  render(<RunDetail />);
  // 页面标题按钮是「已加载」的稳定标记（不在 Tabs 里）。
  await screen.findByRole('button', { name: 'evolutionConsole.backToList' });
};

const renderDetail = (adhoc: CandidateAdhocCase[]) =>
  renderBundle(bundleWithAdhoc(adhoc));

const promoteButton = () =>
  screen.queryByRole('button', { name: 'evolutionConsole.promoteAdhocAction' });

const openPromoteModal = () => {
  const button = promoteButton();
  expect(button).toBeTruthy();
  fireEvent.click(button as HTMLElement);
};

describe('RunDetail · 提升 ad-hoc 语料', () => {
  it('没有 verified case 时不显示提升按钮', async () => {
    await renderDetail([unverified('c_un', '未验证问题')]);

    expect(promoteButton()).toBeNull();
  });

  it('有 verified case 时显示提升按钮', async () => {
    await renderDetail([verified('c1', '已验证问题')]);

    expect(promoteButton()).toBeTruthy();
  });

  it('弹窗只列 verified，默认全选（unverified 不出现）', async () => {
    await renderDetail([
      verified('c1', '已验证问题'),
      unverified('c_un', '未验证问题'),
    ]);

    openPromoteModal();

    const checked = screen.getByRole('checkbox', { name: /已验证问题/ }) as HTMLInputElement;
    expect(checked.checked).toBe(true);
    expect(screen.queryByText('未验证问题')).toBeNull();
  });

  it('人工验真 case 默认不勾选，并提示没有 SQL 真值', async () => {
    await renderDetail([
      verified('c1', '自动验真问题'),
      humanVerified('c2', '人工验真问题'),
    ]);

    openPromoteModal();

    const auto = screen.getByRole('checkbox', { name: /自动验真问题/ }) as HTMLInputElement;
    const human = screen.getByRole('checkbox', { name: /人工验真问题/ }) as HTMLInputElement;
    expect(auto.checked).toBe(true);
    expect(human.checked).toBe(false);
    expect(screen.getByText('evolutionConsole.promoteAdhocHumanHint n=1')).toBeTruthy();
  });

  it('横幅只统计可提升的 case（人工验真不计入）', async () => {
    await renderDetail([
      verified('c1', '自动验真问题'),
      humanVerified('c2', '人工验真问题'),
    ]);

    expect(screen.getByText('evolutionConsole.promoteAdhocHint n=1')).toBeTruthy();
  });

  it('no_sql_truth 的跳过在 toast 里单独说明', async () => {
    const success = vi.spyOn(message, 'success').mockImplementation((() => undefined) as never);
    promoteAdhoc.mockResolvedValue({
      created: [],
      skipped: [{ case_id: 'c2', reason: 'no_sql_truth' }],
      conflicts: [],
    });
    await renderDetail([verified('c1', '问题一')]);

    openPromoteModal();
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.promoteAdhocSubmit' }));

    await waitFor(() => expect(success).toHaveBeenCalled());
    const text = String(success.mock.calls[0][0]);
    expect(text).toContain('evolutionConsole.promoteAdhocNoTruthSkipped n=1');
  });

  it('人工验真 case 不随默认勾选一起提交', async () => {
    promoteAdhoc.mockResolvedValue({ created: [createdRow('c1')], skipped: [], conflicts: [] });
    await renderDetail([
      verified('c1', '自动验真问题'),
      humanVerified('c2', '人工验真问题'),
    ]);

    openPromoteModal();
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.promoteAdhocSubmit' }));

    await waitFor(() => expect(promoteAdhoc).toHaveBeenCalledWith('run_1', ['c1']));
  });

  it('提交只发送勾选中的 case id', async () => {
    promoteAdhoc.mockResolvedValue({ created: [createdRow('c1')], skipped: [], conflicts: [] });
    await renderDetail([
      verified('c1', '问题一'),
      verified('c2', '问题二'),
    ]);

    openPromoteModal();
    fireEvent.click(screen.getByRole('checkbox', { name: /问题二/ }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.promoteAdhocSubmit' }));

    await waitFor(() => expect(promoteAdhoc).toHaveBeenCalledWith('run_1', ['c1']));
  });

  it('成功 toast 文案带 created / skipped 计数', async () => {
    const success = vi.spyOn(message, 'success').mockImplementation((() => undefined) as never);
    promoteAdhoc.mockResolvedValue({
      created: [createdRow('c1'), createdRow('c2')],
      skipped: [{ case_id: 'c3', reason: 'duplicate' }],
      conflicts: [],
    });
    await renderDetail([verified('c1', '问题一'), verified('c2', '问题二')]);

    openPromoteModal();
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.promoteAdhocSubmit' }));

    await waitFor(() => expect(success).toHaveBeenCalled());
    const text = String(success.mock.calls[0][0]);
    expect(text).toContain('evolutionConsole.promoteAdhocDone');
    expect(text).toContain('created=2');
    expect(text).toContain('skipped=1');
  });

  it('冲突在弹窗内渲染（问句 + 既有 case），并保持打开', async () => {
    vi.spyOn(message, 'success').mockImplementation((() => undefined) as never);
    promoteAdhoc.mockResolvedValue({
      created: [],
      skipped: [],
      conflicts: [{ case_id: 'c1', question: '冲突问句', existing_case_id: 'case_existing' }],
    });
    await renderDetail([verified('c1', '冲突问句')]);

    openPromoteModal();
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.promoteAdhocSubmit' }));

    await screen.findByText(/promoteAdhocConflicts n=1/);
    expect(screen.getByText(/冲突问句 —/)).toBeTruthy();
    expect(screen.getByText('case_existing')).toBeTruthy();
    // 弹窗仍打开，且 OK 已切到「知道了」（确认后不再重复提交）。
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(
      screen.getAllByRole('button').some((b) => b.textContent?.includes(
        'evolutionConsole.promoteAdhocClose',
      )),
    ).toBe(true);
  });

  it('多个候选的同名 case 按 case_id 去重，只出现一次', async () => {
    await renderBundle(bundleWithCandidates([
      { candidate_no: 1, evidence: { adhoc_cases: [verified('c1', '问题一')] } },
      { candidate_no: 2, evidence: { adhoc_cases: [verified('c1', '问题一')] } },
    ]));

    openPromoteModal();

    expect(screen.getAllByRole('checkbox', { name: /问题一/ }).length).toBe(1);
  });
});

describe('RunDetail · 条目渲染（relation / query_rule）', () => {
  it('relation 条目渲染 source.col → target.col 与边名', async () => {
    await renderBundle(bundleWithEntries('relation', [{
      source: 'Jobs',
      source_column: 'job_id',
      target: 'WorkingLifes',
      target_column: 'job_id',
      name: 'REL_JOBS_JOB_ID_WORKINGLIFES_JOB_ID',
    }]));

    expect(screen.getByText('Jobs.job_id → WorkingLifes.job_id')).toBeTruthy();
    expect(screen.getByText('REL_JOBS_JOB_ID_WORKINGLIFES_JOB_ID')).toBeTruthy();
    // 无 description 时回落到 i18n 提示（不硬编码中文）
    expect(screen.getByText('evolutionConsole.relationEntryHint')).toBeTruthy();
    // relation 没有 alias/expression，「人工调整」口不出现
    expect(screen.queryByRole('button', { name: 'evolutionConsole.adjustAction' })).toBeNull();
  });

  it('query_rule 条目保持原渲染', async () => {
    await renderBundle(bundleWithEntries('query_rule', [{
      target_code: 'Orders.amount',
      expression: 'SUM(o.amount)',
    }]));

    expect(screen.getByText('Orders.amount')).toBeTruthy();
    expect(screen.getByText('SUM(o.amount)')).toBeTruthy();
    expect(screen.queryByText('evolutionConsole.relationEntryHint')).toBeNull();
    expect(screen.getByRole('button', { name: 'evolutionConsole.adjustAction' })).toBeTruthy();
  });

  it('relation remove 条目显示删除标签与两端', async () => {
    await renderBundle(bundleWithEntries('relation', [{
      op: 'remove', name: 'REL_A_X_B_Y', source: 'A', source_column: 'x',
      target: 'B', target_column: 'y',
      before: { name: 'REL_A_X_B_Y', origin: 'evolved' },
    }]));

    expect(screen.getByText('evolutionConsole.relationOp.remove')).toBeTruthy();
    expect(screen.getByText(/A\.x → B\.y/)).toBeTruthy();
  });
});

describe('RunDetail · 条目渲染（constraint）', () => {
  it('四种 op 渲染 + 勾选键/预览 picks 用后端 entry_key（add 用生成的 name）', async () => {
    previewAdopt.mockResolvedValue({
      planned: [{
        op: 'update',
        name: 'query_rule_Orders_amount',
        expression: 'SUM(o.amount * 2)',
        before: { expression: 'SUM(o.amount)' },
      }, {
        op: 'add',
        name: 'query_rule_Orders_tax',
        target_code: 'Orders.tax',
        constraint_type: 'query_rule',
        expression: 'SUM(o.tax)',
      }],
      skipped: [],
      backup_hint: 'db://evolution_ledgers/x',
      already_adopted: false,
    });
    await renderBundle(bundleWithEntries('constraint', [
      {
        op: 'update',
        name: 'query_rule_Orders_amount',
        expression: 'SUM(o.amount * 2)',
        before: { expression: 'SUM(o.amount)' },
      },
      { op: 'disable', name: 'query_rule_Orders_status' },
      { op: 'enable', name: 'query_rule_Orders_region' },
      {
        op: 'add',
        name: 'query_rule_Orders_tax',
        target_code: 'Orders.tax',
        constraint_type: 'query_rule',
        expression: 'SUM(o.tax)',
      },
    ]));

    // update：name + 旧表达式可见；新表达式收在 details 里（折叠标题可见）
    expect(screen.getByText('query_rule_Orders_amount')).toBeTruthy();
    expect(screen.getByText(/SUM\(o\.amount\)/)).toBeTruthy();
    expect(screen.getByText('evolutionConsole.constraintNewExpression')).toBeTruthy();
    // disable / enable：图标 + name（图标由 aria 语义承载，文本断言 name 与操作标签）
    expect(screen.getByText('query_rule_Orders_status')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.constraintOp.disable')).toBeTruthy();
    expect(screen.getByText('query_rule_Orders_region')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.constraintOp.enable')).toBeTruthy();
    // add：与 query_rule 同形（target_code + expression）
    expect(screen.getByText('Orders.tax')).toBeTruthy();
    expect(screen.getByText('SUM(o.tax)')).toBeTruthy();
    // add / update 可人工修订；disable / enable 只翻转启用状态，没有调整口
    expect(screen.getAllByRole('button', { name: 'evolutionConsole.adjustAction' })).toHaveLength(2);

    // add 的勾选 value = entry_key（生成的 name），不是 target_code（否则 _pick_entries 1001）
    expect((screen.getByRole('checkbox', { name: /Orders\.tax/ }) as HTMLInputElement).value)
      .toBe('query_rule_Orders_tax');

    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.preview' }));
    await waitFor(() => expect(previewAdopt).toHaveBeenCalled());
    expect(previewAdopt.mock.calls[0][0]).toBe('run_1');
    expect(previewAdopt.mock.calls[0][1]).toEqual({
      picks: [
        'query_rule_Orders_amount',
        'query_rule_Orders_status',
        'query_rule_Orders_region',
        'query_rule_Orders_tax',
      ],
    });
    // 预览 planned 显示 op + name + 新表达式，而不是裸 name
    expect(await screen.findByText('update query_rule_Orders_amount: SUM(o.amount * 2)')).toBeTruthy();
    expect(screen.getByText('add query_rule_Orders_tax: SUM(o.tax)')).toBeTruthy();
  });

  it('constraint update 行可调整，disable 行不可调整', async () => {
    await renderBundle(bundleWithEntries('constraint', [
      {
        op: 'update',
        name: 'query_rule_A_x',
        target_code: 'A.x',
        expression: 'new',
      },
      { op: 'disable', name: 'query_rule_A_x', target_code: 'A.x' },
    ]));

    const buttons = screen.getAllByText('evolutionConsole.adjustAction');
    expect(buttons).toHaveLength(1);
  });

  it('constraint update 的调整弹窗以 name 作 entry_key（不是 target_code）', async () => {
    adjustEntry.mockResolvedValue({ ok: true, rc: 0, revised: '新口径', rationale: '' });
    await renderBundle(bundleWithEntries('constraint', [{
      op: 'update',
      name: 'query_rule_Orders_amount',
      target_code: 'Orders.amount',
      expression: 'SUM(o.amount)',
      before: { expression: 'SUM(o.amount)' },
    }]));

    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adjustAction' }));
    const input = await screen.findByPlaceholderText('evolutionConsole.adjustPlaceholder');
    fireEvent.change(input, { target: { value: '改短一点' } });
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adjustSend' }));

    await waitFor(() => expect(adjustEntry).toHaveBeenCalled());
    expect(adjustEntry.mock.calls[0]).toEqual([
      'run_1',
      { entry_key: 'query_rule_Orders_amount', messages: [{ role: 'user', content: '改短一点' }] },
    ]);
  });
});

describe('RunDetail · 版本链 scope', () => {
  it('mapping 走 abstract 版本链（与 alias 同链）', async () => {
    const bundle = bundleWithEntries('mapping', []);
    await renderBundle({ ...bundle, run: { ...bundle.run, kb: 'kb_demo' } });

    await waitFor(() => expect(listVersions).toHaveBeenCalledWith({
      kb: 'kb_demo',
      scope: 'abstract',
      ds: undefined,
      include_archived: true,
      limit: 200,
    }));
  });

  it('tool_schema 走 abstract 版本链（抽象家族）', async () => {
    const bundle = bundleWithEntries('tool_schema', []);
    await renderBundle({ ...bundle, run: { ...bundle.run, kb: 'kb_demo' } });

    await waitFor(() => expect(listVersions).toHaveBeenCalledWith({
      kb: 'kb_demo',
      scope: 'abstract',
      ds: undefined,
      include_archived: true,
      limit: 200,
    }));
  });
});

describe('RunDetail · 条目渲染（tool_schema）', () => {
  it('渲染工具名 / 描述 / 启停，且不出现人工调整按钮', async () => {
    await renderBundle(bundleWithEntries('tool_schema', [
      {
        op: 'update',
        name: 'resolve_entity',
        description: '新描述',
        before: { description: '旧描述' },
      },
      { op: 'disable', name: 'sample_values' },
      { op: 'enable', name: 'list_columns' },
    ]));

    expect(screen.getByText('resolve_entity')).toBeTruthy();
    expect(screen.getByText(/旧描述 → 新描述/)).toBeTruthy();
    expect(screen.getByText('sample_values')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.toolSchemaOp.disable')).toBeTruthy();
    expect(screen.getByText('list_columns')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.toolSchemaOp.enable')).toBeTruthy();
    // v1 不做 adjust/edit（后端 1001）
    expect(screen.queryByRole('button', { name: 'evolutionConsole.adjustAction' })).toBeNull();
  });

  it('同一工具的 update + disable 两条并存时不产生重复 key 警告', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    await renderBundle(bundleWithEntries('tool_schema', [
      { op: 'update', name: 'sample_values', description: '新描述' },
      { op: 'disable', name: 'sample_values' },
    ]));

    // 两行都渲染（div key 带 op；Checkbox value 仍按 entry_key 保持同名）
    expect(screen.getAllByText('sample_values').length).toBe(2);
    const logged = errorSpy.mock.calls.flat().map(String).join(' ');
    expect(logged).not.toMatch(/same key/i);
    expect(logged).not.toMatch(/Encountered two children/i);
  });
});

describe('RunDetail · 首次采纳 v0 提示', () => {
  const previewResponse = {
    planned: [],
    skipped: [],
    backup_hint: 'db://evolution_ledgers/x',
    already_adopted: false,
  };
  const bundleWithQueryRule = () => bundleWithEntries('query_rule', [{
    target_code: 'Orders.amount',
    expression: 'SUM(o.amount)',
  }]);

  const openPreview = async () => {
    previewAdopt.mockResolvedValue(previewResponse);
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.preview' }));
    await screen.findByRole('dialog');
  };

  it('无父版本时展示「自动补 v0 基线」提示', async () => {
    await renderBundle(bundleWithQueryRule());

    await openPreview();

    expect(screen.getByText('evolutionConsole.firstAdoptV0Hint')).toBeTruthy();
  });

  it('有父版本时不展示提示', async () => {
    const bundle = bundleWithQueryRule();
    await renderBundle({
      ...bundle,
      run: { ...bundle.run, parent_version_id: 'ver_parent_1' },
    });

    await openPreview();

    expect(screen.queryByText('evolutionConsole.firstAdoptV0Hint')).toBeNull();
  });
});

describe('RunDetail · 条目渲染（mapping）', () => {
  it('mapping 条目用复合键且不走 constraint 渲染', async () => {
    await renderBundle(bundleWithEntries('mapping', [
      {
        op: 'update', target_code: '赛道.编号', data_source_id: 'ds',
        entity_name: 'Circuits', attribute_name: 'circuitid',
        before: { filter: 'old' }, after: { filter: 'new' },
      },
      {
        op: 'update', target_code: '赛道.编号', data_source_id: 'ds',
        entity_name: 'Circuits', attribute_name: 'circuitid2',
        before: { filter: 'x' }, after: { filter: 'y' },
      },
    ]));

    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    // 同一抽象属性（target_code 相同）的两个落点不能共用一个勾选值
    expect(new Set(boxes.map((b) => b.value)).size).toBe(2);
    expect(boxes.map((b) => b.value).sort()).toEqual([
      '赛道.编号|ds|Circuits|circuitid',
      '赛道.编号|ds|Circuits|circuitid2',
    ]);
    // mapping 的 op 与 constraint 同形，但不得走 constraint 专属渲染
    expect(screen.queryByText('evolutionConsole.constraintOp.disable')).toBeNull();
    expect(screen.queryByText('evolutionConsole.constraintNewExpression')).toBeNull();
    // 三元组与槽的旧 → 新都可见
    expect(screen.getAllByText('Circuits.circuitid').length).toBeGreaterThan(0);
    expect(screen.getByText(/old → new/)).toBeTruthy();
  });

  it('mapping 预览 planned 显示 op + 目标 + 变化槽', async () => {
    previewAdopt.mockResolvedValue({
      planned: [{
        op: 'update', target_code: '赛道.编号', data_source_id: 'ds',
        entity_name: 'Circuits', attribute_name: 'circuitid',
        filter: 'new', changed: { filter: 'new' }, before: { filter: 'old' },
      }],
      skipped: [],
      backup_hint: 'db://evolution_ledgers/x',
      already_adopted: false,
    });
    await renderBundle(bundleWithEntries('mapping', [{
      op: 'update', target_code: '赛道.编号', data_source_id: 'ds',
      entity_name: 'Circuits', attribute_name: 'circuitid',
      before: { filter: 'old' }, after: { filter: 'new' },
    }]));

    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.preview' }));

    expect(await screen.findByText('update 赛道.编号: filter=new')).toBeTruthy();
  });
});

describe('RunDetail · 条目渲染（synonym）', () => {
  const synonymEntry: PatchEntry = {
    terms: ['销量', '销售额'],
    canonical: 'sales',
    data_source_id: 'ds_1',
    question: '销量是多少',
  };

  it('synonym 条目渲染 terms → canonical，勾选键与后端 entry_key 同口径', async () => {
    await renderBundle(bundleWithEntries('synonym', [synonymEntry]));

    expect(screen.getByText('销量 / 销售额 → sales')).toBeTruthy();
    // 后端 `entry_key(..., "synonym")` = `词1/词2:canonical`（人审 picks 必须逐字对上）
    expect((screen.getByRole('checkbox') as HTMLInputElement).value).toBe('销量/销售额:sales');
    // 值同义词没有 alias/表达式语义，不提供人工调整口
    expect(screen.queryByRole('button', { name: 'evolutionConsole.adjustAction' })).toBeNull();
  });

  it('synonym 走独立版本链（scope=synonym，KB 级无 ds）', async () => {
    const bundle = bundleWithEntries('synonym', []);
    await renderBundle({ ...bundle, run: { ...bundle.run, kb: 'kb_demo' } });

    await waitFor(() => expect(listVersions).toHaveBeenCalledWith({
      kb: 'kb_demo',
      scope: 'synonym',
      ds: undefined,
      include_archived: true,
      limit: 200,
    }));
  });

  it('synonym 验真卡提交 adhoc_verdicts（term/canonical 卡片）', async () => {
    confirmAdopt.mockResolvedValue({ ok: true, adopted: true, rc: 0 });
    fetchAdhocEvidence.mockResolvedValue({
      case_id: 'c1',
      question: '销量是多少',
      term: '销量',
      canonical: 'sales',
      columns: ['n'],
      rows: [[7]],
      row_count: 1,
      rows_hash: 'hash-syn',
      ok: true,
      error: null,
      evidence_id: 'ev-syn',
    });
    previewAdopt.mockResolvedValue({
      planned: [],
      skipped: [],
      backup_hint: 'db://evolution_ledgers/x',
      already_adopted: false,
      adhoc_cases: [{ case_id: 'c1', question: '销量是多少' }],
    });
    await renderBundle(bundleWithEntries('synonym', [synonymEntry]));

    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.preview' }));

    // synonym 也走验真卡（不是补 truth）
    fireEvent.click(await screen.findByRole(
      'button', { name: 'evolutionConsole.adhocVerifyViewData' }, { timeout: 15000 },
    ));
    await screen.findByText('sales', undefined, { timeout: 15000 });
    fireEvent.click(screen.getByRole('radio', { name: 'evolutionConsole.adhocVerifyPass' }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adhocVerifySubmit' }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.confirmWrite' }));

    await waitFor(() => expect(confirmAdopt).toHaveBeenCalled(), { timeout: 15000 });
    const body = confirmAdopt.mock.calls[0][1];
    expect(body.adhoc_verdicts).toEqual([
      {
        case_id: 'c1', verdict: 'pass', evidence_id: 'ev-syn',
        row_count: 1, rows_hash: 'hash-syn',
      },
    ]);
    expect(body).not.toHaveProperty('adhoc_truths');
    expect(fetchAdhocEvidence).toHaveBeenCalledWith('run_1', 'c1', ['销量/销售额:sales']);
  }, 60000);
});

describe('RunDetail · ad-hoc 验真（alias）与 truth（其它 kind）', () => {
  const previewWithAdhoc = {
    planned: [],
    skipped: [],
    backup_hint: 'db://evolution_ledgers/x',
    already_adopted: false,
    adhoc_cases: [{ case_id: 'c1', question: '问题一' }],
  };

  const evidence = {
    case_id: 'c1',
    question: '问题一',
    alias: 'job_count',
    concept: '职位数量',
    concept_aliases: [],
    columns: ['n'],
    rows: [[1]],
    row_count: 1,
    rows_hash: 'hash-1',
    ok: true,
    error: null,
    evidence_id: 'ev-1',
  };

  const submitPreview = () => {
    previewAdopt.mockResolvedValue(previewWithAdhoc);
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.preview' }));
  };

  const confirmButton = () =>
    screen.getByRole('button', { name: 'evolutionConsole.confirmWrite' }) as HTMLButtonElement;

  it('alias run 的 ad-hoc 卡提交后采纳请求带 adhoc_verdicts', async () => {
    confirmAdopt.mockResolvedValue({ ok: true, adopted: true, rc: 0 });
    fetchAdhocEvidence.mockResolvedValue(evidence);
    await renderBundle(bundleWithEntries('alias', [{
      concept: '职位数量', alias: 'job_count',
    }]));

    submitPreview();

    // alias 走验真卡：取数 → 判通过 → 提交
    fireEvent.click(await screen.findByRole(
      'button', { name: 'evolutionConsole.adhocVerifyViewData' }, { timeout: 15000 },
    ));
    await screen.findByText('职位数量', undefined, { timeout: 15000 });
    fireEvent.click(screen.getByRole('radio', { name: 'evolutionConsole.adhocVerifyPass' }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adhocVerifySubmit' }));

    fireEvent.click(confirmButton());

    await waitFor(() => expect(confirmAdopt).toHaveBeenCalled(), { timeout: 15000 });
    const body = confirmAdopt.mock.calls[0][1];
    expect(body.adhoc_verdicts).toEqual([
      {
        case_id: 'c1', verdict: 'pass', evidence_id: 'ev-1',
        row_count: 1, rows_hash: 'hash-1',
      },
    ]);
    expect(body).not.toHaveProperty('adhoc_truths');
    // 验真取数带的是本次勾选的 picks（票据绑定它）
    expect(fetchAdhocEvidence).toHaveBeenCalledWith(
      'run_1', 'c1', ['职位数量:job_count'],
    );
  }, 60000);

  it('非 alias run 仍提交 adhoc_truths', async () => {
    confirmAdopt.mockResolvedValue({ ok: true, adopted: true, rc: 0 });
    await renderBundle(bundleWithEntries('query_rule', [{
      target_code: 'Orders.amount', expression: 'SUM(o.amount)',
    }]));

    submitPreview();

    // 非 alias 仍走补 truth 弹窗
    const textarea = await screen.findByPlaceholderText(
      'evolutionConsole.adhocControlPlaceholder', undefined, { timeout: 15000 },
    );
    fireEvent.change(textarea, { target: { value: 'SELECT 1' } });
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adhocSubmit' }));

    fireEvent.click(confirmButton());

    await waitFor(() => expect(confirmAdopt).toHaveBeenCalled(), { timeout: 15000 });
    const body = confirmAdopt.mock.calls[0][1];
    expect(body.adhoc_truths).toEqual([
      { case_id: 'c1', control: ['SELECT 1'], expect: 'answer' },
    ]);
    expect(body).not.toHaveProperty('adhoc_verdicts');
  }, 60000);

  it('勾选变化清空已填判定并重置验真弹窗', async () => {
    fetchAdhocEvidence.mockResolvedValue(evidence);
    await renderBundle(bundleWithEntries('alias', [
      { concept: '职位数量', alias: 'job_count' },
      { concept: '城市数量', alias: 'city_count' },
    ]));

    submitPreview();
    fireEvent.click(await screen.findByRole(
      'button', { name: 'evolutionConsole.adhocVerifyViewData' }, { timeout: 15000 },
    ));
    await screen.findByText('职位数量', undefined, { timeout: 15000 });
    fireEvent.click(screen.getByRole('radio', { name: 'evolutionConsole.adhocVerifyPass' }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adhocVerifySubmit' }));

    // 判定已就绪：预览弹窗允许确认（alias 文案）
    expect(await screen.findByText(
      'evolutionConsole.previewAdhocReadyVerify', undefined, { timeout: 15000 },
    )).toBeTruthy();
    expect(confirmButton().disabled).toBe(false);

    // picks 变化 ⇒ 票据失效，判定全部作废
    fireEvent.click(screen.getByRole('checkbox', { name: /城市数量/ }));

    expect(await screen.findByText(
      'evolutionConsole.previewAdhocNeedVerify', undefined, { timeout: 15000 },
    )).toBeTruthy();
    expect(confirmButton().disabled).toBe(true);
  }, 60000);

  it('selected 被程序性裁剪（刷新）时同样清空判定', async () => {
    fetchAdhocEvidence.mockResolvedValue(evidence);
    await renderBundle(bundleWithEntries('alias', [
      { concept: '职位数量', alias: 'job_count' },
      { concept: '城市数量', alias: 'city_count' },
    ]));

    submitPreview();
    fireEvent.click(await screen.findByRole(
      'button', { name: 'evolutionConsole.adhocVerifyViewData' }, { timeout: 15000 },
    ));
    await screen.findByText('职位数量', undefined, { timeout: 15000 });
    fireEvent.click(screen.getByRole('radio', { name: 'evolutionConsole.adhocVerifyPass' }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adhocVerifySubmit' }));

    expect(await screen.findByText(
      'evolutionConsole.previewAdhocReadyVerify', undefined, { timeout: 15000 },
    )).toBeTruthy();
    expect(confirmButton().disabled).toBe(false);

    // 服务端条目集合变了：load() 会裁剪 selected（不是用户点勾选）
    getRunBundle.mockResolvedValue(bundleWithEntries('alias', [
      { concept: '职位数量', alias: 'job_count' },
    ]));
    fireEvent.click(screen.getByRole('button', { name: /common\.refresh/ }));
    await waitFor(
      () => expect(screen.queryByRole('checkbox', { name: /城市数量/ })).toBeNull(),
      { timeout: 15000 },
    );

    // stale verdicts 必须被清：回落到「待验真」，确认按钮禁用
    expect(await screen.findByText(
      'evolutionConsole.previewAdhocNeedVerify', undefined, { timeout: 15000 },
    )).toBeTruthy();
    expect(confirmButton().disabled).toBe(true);
  }, 60000);

  it('取消勾选相关条目后验真卡不再列出该 case，采纳仍可完成', async () => {
    // Critical 1：服务端按 picks 过滤 adhoc_cases（这里用 mock 镜像同口径）。
    // 取消勾选「城市数量」后，c2 不再出现在卡片上，也不会死锁采纳。
    confirmAdopt.mockResolvedValue({ ok: true, adopted: true, rc: 0 });
    fetchAdhocEvidence.mockResolvedValue(evidence);
    previewAdopt.mockImplementation(
      (_runId: string, body: { picks?: string[] }) => {
        const picks = body.picks ?? [];
        const adhoc: { case_id: string; question: string }[] = [];
        if (picks.includes('职位数量:job_count')) adhoc.push({ case_id: 'c1', question: '问题一' });
        if (picks.includes('城市数量:city_count')) adhoc.push({ case_id: 'c2', question: '问题二' });
        return Promise.resolve({
          planned: [],
          skipped: [],
          backup_hint: 'db://evolution_ledgers/x',
          already_adopted: false,
          adhoc_cases: adhoc,
        });
      },
    );
    await renderBundle(bundleWithEntries('alias', [
      { concept: '职位数量', alias: 'job_count' },
      { concept: '城市数量', alias: 'city_count' },
    ]));

    // 取消勾选「城市数量」（其问句映射到 c2），再重新预览
    fireEvent.click(screen.getByRole('checkbox', { name: /城市数量/ }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.preview' }));

    await screen.findByRole(
      'button', { name: 'evolutionConsole.adhocVerifyViewData' }, { timeout: 15000 },
    );
    // 卡片只列 c1（c2 完全不在验真卡里）
    expect(screen.queryByText('c2')).toBeNull();

    // 判 c1 通过 → 可确认采纳
    fireEvent.click(screen.getByRole('button', {
      name: 'evolutionConsole.adhocVerifyViewData',
    }));
    await screen.findByText('职位数量', undefined, { timeout: 15000 });
    fireEvent.click(screen.getByRole('radio', { name: 'evolutionConsole.adhocVerifyPass' }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adhocVerifySubmit' }));

    expect(confirmButton().disabled).toBe(false);
    fireEvent.click(confirmButton());

    await waitFor(() => expect(confirmAdopt).toHaveBeenCalled(), { timeout: 15000 });
    const body = confirmAdopt.mock.calls[0][1];
    expect(body.picks).toEqual(['职位数量:job_count']);
    expect(body.adhoc_verdicts).toEqual([
      {
        case_id: 'c1', verdict: 'pass', evidence_id: 'ev-1',
        row_count: 1, rows_hash: 'hash-1',
      },
    ]);
  }, 60000);

  it('取消勾选全部待验真条目后仍发送空 adhoc_verdicts 并完成采纳', async () => {
    // 残项 Critical 1：alias 批即使没有任何待验真 case，也必须带
    // `adhoc_verdicts: []`；整键缺席会让后端回落到 truths 覆盖校验并 1001。
    confirmAdopt.mockResolvedValue({ ok: true, adopted: true, rc: 0 });
    previewAdopt.mockImplementation(
      (_runId: string, body: { picks?: string[] }) => {
        const picks = body.picks ?? [];
        return Promise.resolve({
          planned: [],
          skipped: [],
          backup_hint: 'db://evolution_ledgers/x',
          already_adopted: false,
          adhoc_cases: picks.includes('部门数量:dept_count')
            ? [{ case_id: 'c2', question: '问题二' }]
            : [],
        });
      },
    );
    await renderBundle(bundleWithEntries('alias', [
      { concept: '职位数量', alias: 'job_count' },
      { concept: '部门数量', alias: 'dept_count' },
    ]));

    fireEvent.click(screen.getByRole('checkbox', { name: /部门数量/ }));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.preview' }));

    // 没有待验真 case：直接进确认弹窗，无需判定
    await waitFor(() => expect(confirmButton().disabled).toBe(false), { timeout: 15000 });
    fireEvent.click(confirmButton());

    await waitFor(() => expect(confirmAdopt).toHaveBeenCalled(), { timeout: 15000 });
    const body = confirmAdopt.mock.calls[0][1];
    expect(body.picks).toEqual(['职位数量:job_count']);
    expect(body.adhoc_verdicts).toEqual([]);
    expect(body).not.toHaveProperty('adhoc_truths');
  }, 60000);

  it('判定不通过时确认禁用且不请求服务端', async () => {
    await renderBundle(bundleWithEntries('alias', [{
      concept: '职位数量', alias: 'job_count',
    }]));

    submitPreview();

    // 不通过不需要先看数据：直接判定即可（风险由「看数据才能通过」兜住）
    fireEvent.click(await screen.findByRole(
      'radio', { name: 'evolutionConsole.adhocVerifyFail' }, { timeout: 15000 },
    ));
    fireEvent.click(screen.getByRole('button', { name: 'evolutionConsole.adhocVerifySubmit' }));

    // 预览弹窗给出否决提示，确认按钮禁用
    expect(await screen.findByText(
      'evolutionConsole.previewAdhocFailVeto', undefined, { timeout: 15000 },
    )).toBeTruthy();
    expect(confirmButton().disabled).toBe(true);

    fireEvent.click(confirmButton());
    expect(confirmAdopt).not.toHaveBeenCalled();
  }, 60000);
});
