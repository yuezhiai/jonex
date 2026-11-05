// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { message } from 'antd';
import StartRunModal, { DEFAULT_JINPIN_DS, DEFAULT_KB } from './StartRunModal';
import type { OptimizableOptions } from '../../api/nl2sqlKbOptions';
import type { CompiledConstraint, EvolutionToolOption } from '../../api/evolution';

// `vi.mock` 工厂会被提升到 import 之前，桩必须用 vi.hoisted 声明（避免 TDZ）。
const {
  startEvolutionRun, listCorpusCases, fetchOptimizableOptions, getCompiledConstraints,
  getAbstractSchema, getCompiledSchema, listEvolutionToolOptions,
} = vi.hoisted(() => ({
  startEvolutionRun: vi.fn(),
  listCorpusCases: vi.fn(),
  fetchOptimizableOptions: vi.fn(),
  getCompiledConstraints: vi.fn(),
  getAbstractSchema: vi.fn(),
  getCompiledSchema: vi.fn(),
  listEvolutionToolOptions: vi.fn(),
}));

vi.mock('../../api/evolution', () => ({
  startEvolutionRun,
  listCorpusCases,
  getCompiledConstraints,
  getAbstractSchema,
  getCompiledSchema,
  listEvolutionToolOptions,
}));

vi.mock('../../api/nl2sqlKbOptions', () => ({
  EMPTY_OPTIONS: { aliasKbs: [], queryRuleKbs: [] },
  fetchOptimizableOptions,
}));

// t 必须是稳定引用：组件的 useCallback 链以它为依赖。
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
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

/** 默认值必须出现在池里，否则值对账 effect 会把 kb/ds 清空（那是另一条用例的前提）。 */
const optionsWithDefaults = (): OptimizableOptions => ({
  aliasKbs: [{ id: DEFAULT_KB, name: 'KB assets' }],
  queryRuleKbs: [
    { kb: { id: DEFAULT_KB, name: 'KB assets' }, sources: [{ id: DEFAULT_JINPIN_DS, name: 'jinpin' }] },
  ],
});

/** 同一个 KB 下一个可用源都没有：relation/query_rule 的 ds 必填校验因此必然失败。 */
const optionsWithoutSources = (): OptimizableOptions => ({
  aliasKbs: [{ id: DEFAULT_KB, name: 'KB assets' }],
  queryRuleKbs: [{ kb: { id: DEFAULT_KB, name: 'KB assets' }, sources: [] }],
});

/** antd 的 Radio/Button 需要完整指针序列（fireEvent.click 不会驱动 Radio.Group 的受控值）。 */
const setup = async (
  options: OptimizableOptions = optionsWithDefaults(),
  constraints: CompiledConstraint[] = [],
) => {
  // constraint 表让 DOM 变大后，user-event 的 pointer-events 计算（jsdom getComputedStyle）
  // 会拖到 20s 超时边缘；这里跳过该项检查（真实事件派发不变）。
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  fetchOptimizableOptions.mockResolvedValue(options);
  listCorpusCases.mockResolvedValue({ target: {}, cases: [] });
  getCompiledConstraints.mockResolvedValue({ constraints });
  getAbstractSchema.mockResolvedValue({ schema: { entities: [], relations: [] } });
  getCompiledSchema.mockResolvedValue({ entity_types: [] });
  listEvolutionToolOptions.mockResolvedValue({ tools: [], source: 'code' });
  render(
    <StartRunModal open onClose={() => {}} onStarted={() => {}} onFailed={() => {}} />,
  );
  // 选项拉取完成前 OK 是禁用的：等到解除即「本轮选项已就绪」。
  await waitFor(() => {
    const ok = screen.getByRole('button', { name: 'evolutionConsole.startRun' }) as HTMLButtonElement;
    expect(ok.disabled).toBe(false);
  });
  return user;
};

/** antd 把真实的 radio input 藏成 `pointer-events: none`，指针事件要打在 label 包装上。 */
const relationOption = () =>
  screen.getByRole('radio', { name: /evolutionConsole\.goal\.relation/ }).closest('label') as HTMLElement;

const submitButton = () =>
  screen.getByRole('button', { name: 'evolutionConsole.startRun' }) as HTMLButtonElement;

const constraintOption = () =>
  screen.getByRole('radio', { name: /evolutionConsole\.goal\.constraint/ }).closest('label') as HTMLElement;

const mappingOption = () =>
  screen.getByRole('radio', { name: /evolutionConsole\.goal\.mapping/ }).closest('label') as HTMLElement;

const toolSchemaOption = () =>
  screen.getByRole('radio', { name: /evolutionConsole\.goal\.tool_schema/ })
    .closest('label') as HTMLElement;

/** antd Select：mouseDown 打开浮层（浮层挂在 body），再点选项。 */
const pickSelectOption = async (label: string, optionText: string) => {
  fireEvent.mouseDown(screen.getByLabelText(label));
  fireEvent.click(await screen.findByText(optionText));
};

/** constraint 行的操作是 antd Radio.Group：按行的 target_code 定位到该行，再点 label。 */
const chooseConstraintOp = async (
  user: ReturnType<typeof userEvent.setup>,
  rowTargetCode: string,
  op: 'keep' | 'disable' | 'enable' | 'update',
) => {
  const row = screen.getByText(rowTargetCode).closest('tr') as HTMLElement;
  const radio = within(row).getByRole('radio', { name: `evolutionConsole.constraintOp.${op}` });
  await user.click(radio.closest('label') as HTMLElement);
};

describe('StartRunModal · relation kind', () => {
  it('kind 选项里列出 relation', async () => {
    await setup();

    expect(relationOption()).toBeTruthy();
  });

  it('切到 relation 后仍显示 ds（与 query_rule 同校验）且不显示 picks', async () => {
    const user = await setup();

    await user.click(relationOption());

    // ds 表单标签 + 占位符都走 evolutionConsole.formDs
    expect((await screen.findAllByText('evolutionConsole.formDs')).length).toBeGreaterThan(0);
    expect(screen.queryByPlaceholderText('SysRole.f_category, Jobs.f_status')).toBeNull();
  });

  it('提交 relation 时只发 kb + ds，不发 picks', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_rel_1' });
    const user = await setup();

    await user.click(relationOption());
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    const body = startEvolutionRun.mock.calls[0][0];
    expect(body.kind).toBe('relation');
    expect(body.kb).toBe(DEFAULT_KB);
    expect(body.ds).toBe(DEFAULT_JINPIN_DS);
    expect(body.picks).toBeUndefined();
  });

  it('relation 缺 ds 时校验拦截（不发请求）', async () => {
    const user = await setup(optionsWithoutSources());

    await user.click(relationOption());
    // 值对账会清掉不在池里的 ds；随后提交必须被 required 校验拦住。
    await user.click(submitButton());
    await screen.findByText('evolutionConsole.formDsRequired');

    expect(startEvolutionRun).not.toHaveBeenCalled();
  });

  it('relation：勾选现有边后提交 remove entries', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_rel_remove' });
    const user = await setup();
    // setup 会把 getCompiledSchema 先设成空 entity_types；这里覆盖成带 join 边的编译行。
    getCompiledSchema.mockResolvedValue({ relation_types: [{
      name: 'REL_A_X_B_Y', source: 'A', source_column: 'x',
      target: 'B', target_column: 'y', origin: 'evolved' }] });

    await user.click(relationOption());
    await user.click(await screen.findByLabelText('relation-edge-REL_A_X_B_Y'));
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    const arg = startEvolutionRun.mock.calls[startEvolutionRun.mock.calls.length - 1]![0];
    expect(arg.kind).toBe('relation');
    expect(arg.entries).toEqual([{ op: 'remove', name: 'REL_A_X_B_Y' }]);
    expect(getCompiledSchema).toHaveBeenCalledWith(DEFAULT_KB, DEFAULT_JINPIN_DS);
  });

  it('relation：一条边都不勾选 → 不送 entries（发现器模式）', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_rel_discover' });
    const user = await setup();
    getCompiledSchema.mockResolvedValue({ relation_types: [{
      name: 'REL_A_X_B_Y', source: 'A', source_column: 'x',
      target: 'B', target_column: 'y', origin: 'evolved' }] });

    await user.click(relationOption());
    // 等边表渲染出来，证明确实有可勾的边，但保持零勾选。
    await screen.findByLabelText('relation-edge-REL_A_X_B_Y');
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    expect(startEvolutionRun.mock.calls[0][0].entries).toBeUndefined();
  });
});

describe('StartRunModal · constraint kind', () => {
  const currentConstraints = () => [
    {
      name: 'query_rule_Orders_amount',
      target_type: 'attribute',
      target_code: 'Orders.amount',
      constraint_type: 'query_rule',
      expression: 'SUM(o.amount)',
    },
    {
      name: 'query_rule_Orders_status',
      target_type: 'attribute',
      target_code: 'Orders.status',
      constraint_type: 'query_rule',
      expression: "'unknown'",
      enabled: false,
    },
    {
      name: 'query_rule_Orders_region',
      target_type: 'attribute',
      target_code: 'Orders.region',
      constraint_type: 'query_rule',
      expression: "'CN'",
    },
  ];

  it('kind 选项里有 constraint；按 kb + ds 读取约束并渲染（含启用状态）；零变化不发请求', async () => {
    const user = await setup(optionsWithDefaults(), currentConstraints());

    expect(constraintOption()).toBeTruthy();
    await user.click(constraintOption());

    expect(await screen.findByText('Orders.amount')).toBeTruthy();
    expect(screen.getByText('SUM(o.amount)')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.constraintDisabledTag')).toBeTruthy();
    // amount / region 两条启用行
    expect(screen.getAllByText('evolutionConsole.constraintEnabledTag').length).toBe(2);
    expect(getCompiledConstraints).toHaveBeenCalledWith(DEFAULT_KB, DEFAULT_JINPIN_DS);

    // 没有任何变更：entries 为空 → 后端必拒（1001），前端直接拦下
    await user.click(submitButton());
    expect(startEvolutionRun).not.toHaveBeenCalled();
  });

  it('只提交变化行：启用行可停用、停用行可启用，未改的行不发', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_c_1' });
    const user = await setup(optionsWithDefaults(), currentConstraints());

    await user.click(constraintOption());
    await screen.findByText('Orders.amount');
    // amount（启用）→ 停用；status（已停用）→ 启用；region 保持「保持不变」。
    await chooseConstraintOp(user, 'Orders.amount', 'disable');
    await chooseConstraintOp(user, 'Orders.status', 'enable');
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    const body = startEvolutionRun.mock.calls[0][0];
    expect(body.kind).toBe('constraint');
    expect(body.kb).toBe(DEFAULT_KB);
    expect(body.ds).toBe(DEFAULT_JINPIN_DS);
    expect(body.picks).toBeUndefined();
    // 按约束行顺序；Orders.region 未改动 → 不出现
    expect(body.entries).toEqual([
      { op: 'disable', name: 'query_rule_Orders_amount' },
      { op: 'enable', name: 'query_rule_Orders_status' },
    ]);
  });

  it('改口径 + 新增口径：只发 update/add 两类条目（表达式未变的 update 不发）', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_c_2' });
    const user = await setup(optionsWithDefaults(), currentConstraints());

    await user.click(constraintOption());
    await screen.findByText('Orders.amount');
    await chooseConstraintOp(user, 'Orders.amount', 'update');
    // region 也切「改口径」但表达式不动 → 不应产生 update 条目
    await chooseConstraintOp(user, 'Orders.region', 'update');
    const textarea = await screen.findByLabelText('constraint-expr-query_rule_Orders_amount');
    fireEvent.change(textarea, { target: { value: 'SUM(o.amount * 2)' } });
    fireEvent.change(screen.getByLabelText('constraint-add-target'), { target: { value: 'Orders.tax' } });
    fireEvent.change(screen.getByLabelText('constraint-add-expression'), {
      target: { value: 'SUM(o.tax)' },
    });
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    expect(startEvolutionRun.mock.calls[0][0].entries).toEqual([
      { op: 'update', name: 'query_rule_Orders_amount', expression: 'SUM(o.amount * 2)' },
      { op: 'add', target_code: 'Orders.tax', constraint_type: 'query_rule', expression: 'SUM(o.tax)' },
    ]);
  });
});

describe('StartRunModal · tool_schema kind', () => {
  const toolOptions = (): EvolutionToolOption[] => [
    {
      name: 'resolve_entity',
      description: '默认：解析实体',
      effective_description: '默认：解析实体',
      enabled: true,
      overridden: false,
    },
    {
      name: 'sample_values',
      description: '默认：采样',
      effective_description: '默认：采样',
      enabled: true,
      overridden: false,
    },
    {
      name: 'list_columns',
      description: '默认：列',
      effective_description: '覆盖：列',
      enabled: false,
      overridden: true,
    },
  ];

  it('只提交变化：改描述 → update，勾停用 → disable，取消停用 → enable', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_t_1' });
    const user = await setup();
    // setup 会先铺一份空清单，测试自己的桩必须在 setup 之后覆盖（否则被清空）。
    listEvolutionToolOptions.mockResolvedValue({ tools: toolOptions(), source: 'abstract' });

    expect(toolSchemaOption()).toBeTruthy();
    await user.click(toolSchemaOption());

    expect((await screen.findByText('resolve_entity'))).toBeTruthy();
    expect(screen.getByText('覆盖：列')).toBeTruthy();
    expect(listEvolutionToolOptions).toHaveBeenCalledWith(DEFAULT_KB);

    fireEvent.change(screen.getByLabelText('tool-desc-resolve_entity'), {
      target: { value: '新描述' },
    });
    await user.click(screen.getByLabelText('tool-disable-sample_values'));
    // list_columns 当前已停用（覆盖项 enabled:false）→ 取消勾选 = 启用
    await user.click(screen.getByLabelText('tool-disable-list_columns'));
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    const body = startEvolutionRun.mock.calls[0][0];
    expect(body.kind).toBe('tool_schema');
    expect(body.kb).toBe(DEFAULT_KB);
    // abstract 家族：KB 级 run，后端拒 ds
    expect(body.ds).toBeUndefined();
    // 按工具行顺序；每个工具只发本次真正变化的那条，无多余条目
    expect(body.entries).toEqual([
      { op: 'update', name: 'resolve_entity', description: '新描述' },
      { op: 'disable', name: 'sample_values' },
      { op: 'enable', name: 'list_columns' },
    ]);
  });

  it('新描述输入限制 256 字符（与后端截断同口径）', async () => {
    const user = await setup();
    listEvolutionToolOptions.mockResolvedValue({ tools: toolOptions(), source: 'code' });

    await user.click(toolSchemaOption());
    const input = await screen.findByLabelText('tool-desc-resolve_entity');

    expect(input.getAttribute('maxlength')).toBe('256');
  });

  it('工具名单加载失败时禁用提交（避免只送出空 entries）', async () => {
    const user = await setup();
    listEvolutionToolOptions.mockRejectedValue(new Error('boom'));

    await user.click(toolSchemaOption());
    await screen.findByText('evolutionConsole.toolSchemaLoadFailed');

    expect(submitButton().disabled).toBe(true);
  });

  it('描述与启停都未变化时不发请求（提示）', async () => {
    const warning = vi.spyOn(message, 'warning').mockImplementation((() => undefined) as never);
    const user = await setup();
    listEvolutionToolOptions.mockResolvedValue({ tools: toolOptions(), source: 'code' });

    await user.click(toolSchemaOption());
    await screen.findByText('resolve_entity');
    await user.click(submitButton());

    expect(startEvolutionRun).not.toHaveBeenCalled();
    expect(warning).toHaveBeenCalled();
  });
});

describe('StartRunModal · synonym kind', () => {
  /** 有编译 schema（queryRuleKbs）但**没有**抽象 schema（aliasKbs 空）：
   * synonym 的候选只依赖失败观测、验真走编译 schema，这类 KB 必须能选。 */
  const optionsCompiledOnly = (): OptimizableOptions => ({
    aliasKbs: [],
    queryRuleKbs: [
      { kb: { id: DEFAULT_KB, name: 'KB assets' }, sources: [{ id: DEFAULT_JINPIN_DS, name: 'jinpin' }] },
    ],
  });

  const synonymOption = () =>
    screen.getByRole('radio', { name: /evolutionConsole\.goal\.synonym/ })
      .closest('label') as HTMLElement;

  it('kind 选项里列出 synonym；切过去后不显示 ds，但保留窗口天数', async () => {
    const user = await setup();

    expect(synonymOption()).toBeTruthy();
    await user.click(synonymOption());

    expect(screen.queryByText('evolutionConsole.formDs')).toBeNull();
    expect(screen.getByText('evolutionConsole.formWindowDays')).toBeTruthy();
  });

  it('提交 synonym 时只发 kb（KB 级 run，不发 ds / entries）', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_syn_1' });
    const user = await setup();

    await user.click(synonymOption());
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    const body = startEvolutionRun.mock.calls[0][0];
    expect(body.kind).toBe('synonym');
    expect(body.kb).toBe(DEFAULT_KB);
    expect(body.ds).toBeUndefined();
    expect(body.entries).toBeUndefined();
    expect(body.window_days).toBe(7);
  });

  it('KB 池是两族并集：没有抽象 schema、只有编译 schema 的 KB 也可选', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_syn_2' });
    const user = await setup(optionsCompiledOnly());

    await user.click(synonymOption());
    // 只存在于 queryRuleKbs 的 KB 必须出现在下拉里（旧实现只读 aliasKbs，这里会找不到）。
    await pickSelectOption('evolutionConsole.formKb', `KB assets（${DEFAULT_KB}）`);
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    const body = startEvolutionRun.mock.calls[0][0];
    expect(body.kind).toBe('synonym');
    expect(body.kb).toBe(DEFAULT_KB);
    expect(body.ds).toBeUndefined();
  });
});

describe('StartRunModal · mapping kind', () => {
  /** mapping 的 ds 池复用 queryRuleKbs.sources；默认 ds 不在池里时会被对账清空，
   * 所以这里显式给出 ds_1 并走真实的选择交互。 */
  const mappingOptions = (): OptimizableOptions => ({
    aliasKbs: [{ id: DEFAULT_KB, name: 'KB assets' }],
    queryRuleKbs: [
      { kb: { id: DEFAULT_KB, name: 'KB assets' }, sources: [{ id: 'ds_1', name: 'circuits' }] },
    ],
  });

  const abstractWithLanding = () => ({
    schema: {
      entities: [{
        name: '赛道', aliases: [],
        sources: [{ dataSourceId: 'ds_1', entityName: 'Circuits', included: true }],
        attributes: [{
          name: '编号', type: 'string', primaryKey: false, required: false,
          sources: [{
            dataSourceId: 'ds_1', entityName: 'Circuits',
            attributeName: 'circuitid', filter: 'old', stale: false,
          }],
        }],
      }],
      relations: [],
    },
  });

  it('mapping：读落点、只提交变化行', async () => {
    startEvolutionRun.mockResolvedValue({ run_id: 'run_m_1' });
    const user = await setup(mappingOptions());
    // 必须在 setup 之后覆盖：setup 会把 getAbstractSchema 先设成空池。
    getAbstractSchema.mockResolvedValue(abstractWithLanding());

    await user.click(mappingOption());
    // 默认 ds 不在池里 → 选 ds_1；落点行按当前 ds 过滤
    await pickSelectOption('evolutionConsole.formDs', 'circuits（ds_1）');

    const input = await screen.findByLabelText('mapping-filter-Circuits.circuitid');
    fireEvent.change(input, { target: { value: 'new' } });
    await user.click(submitButton());

    await waitFor(() => expect(startEvolutionRun).toHaveBeenCalledTimes(1));
    const arg = startEvolutionRun.mock.calls[0][0];
    expect(arg.kind).toBe('mapping');
    // mapping 是 KB 级 run：后端拒 ds
    expect(arg.ds).toBeUndefined();
    expect(arg.entries).toEqual([expect.objectContaining({
      op: 'update', target_code: '赛道.编号', data_source_id: 'ds_1',
      entity_name: 'Circuits', attribute_name: 'circuitid', filter: 'new',
    })]);
  });
});
