/**
 * 「概念对齐」页签的组件级回归（jsdom + RTL）。
 *
 * 钉死两件事：
 * - **T6-2 生成成功 / 重拉失败解耦**：生成已写库成功，重拉失败必须**仍走成功路径**
 *   （success toast + `refreshAfterGenerateFailed` 警告 + 用 generate 回包续渲染），
 *   绝不落进外层 catch 报「生成失败」——那会让用户重试一次、对数据库是第二次覆盖生成。
 * - **B2 聚焦剔除计数**：`focus.droppedSeeds > 0` 才发剔除计数警告 toast；为 0 时
 *   只有「聚焦生成完成」info，不多嘴。
 *
 * 断言一律按 i18n key（`react-i18next` 被 mock 成直出键名，带参数则附 JSON），
 * 不锁中文文案。antd message 渲染在 document.body，用例间 `message.destroy()` 防串扰。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { message } from 'antd';

import AbstractSchemaTab from './AbstractSchemaTab';
import {
  generateAbstractSchema,
  getAbstractSchema,
  previewFocusAbstractSchema,
} from '@/api/abstractSchema';
import { getDomainKnowledgeDataSources } from '@/api/domainKnowledge';
import { listAlignments } from '@/api/ontologyAlignment';
import type {
  AbstractEntity,
  AbstractSchema,
  FocusSummary,
  GenerateResult,
} from '@/api/abstractSchema';
import type { OntologyDataAlignment } from '@/api/ontologyAlignment';

// 恒等的 t（见 FocusGenerateModal.test.tsx 里的同款说明：组件拿它当 effect 依赖）
const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

vi.mock('@/api/abstractSchema', () => ({
  getAbstractSchema: vi.fn(),
  generateAbstractSchema: vi.fn(),
  saveAbstractSchema: vi.fn(),
  previewFocusAbstractSchema: vi.fn(),
}));

// 页签内嵌「数据资产对齐」区（P1.5b）：本文件只测概念对齐，不让它去打真实网络。
// 空列表 = 该区落 empty 态，对既有断言没有影响。
vi.mock('@/api/ontologyAlignment', () => ({
  listAlignments: vi.fn(async () => []),
  suggestAlignments: vi.fn(),
  setAlignmentStatus: vi.fn(),
}));

// 对齐区会经 `getDomainKnowledgeDataSources` 解析数据源展示名（纯展示增强）：
// 不 mock 的话组件挂载时会打真实网络。默认空列表 → 行上回退成短 id。
vi.mock('@/api/domainKnowledge', () => ({
  getDomainKnowledgeDataSources: vi.fn(async () => []),
}));

const getMock = vi.mocked(getAbstractSchema);
const genMock = vi.mocked(generateAbstractSchema);
const previewMock = vi.mocked(previewFocusAbstractSchema);
const listAlignmentsMock = vi.mocked(listAlignments);
const getSourcesMock = vi.mocked(getDomainKnowledgeDataSources);

// ---------- 夹具 ----------

const ent = (name: string, dataSourceId = 'ds_a'): AbstractEntity => ({
  name,
  description: '',
  aliases: [],
  sources: [{ dataSourceId, entityName: name, included: true, sourceName: `源-${dataSourceId}`, stale: false, staleReason: null }],
  attributes: [],
});

const schemaOf = (
  entities: AbstractEntity[],
  status: 'proposed' | 'active' = 'proposed',
): AbstractSchema => ({
  id: 'as_1',
  knowledgeBaseId: 'kb_1',
  schemaVersion: 1,
  status,
  schema: { entities, relations: [] },
  staleCount: 0,
});

const alignmentOf = (
  over: Partial<OntologyDataAlignment> = {},
): OntologyDataAlignment => ({
  id: 'al_1',
  doc_type: '员工',
  data_source_id: 'ds_a',
  entity_name: 'Employee',
  status: 'proposed',
  ...over,
});

/** generate 回包（`mapGenerateResult` 产出的完整形状：schema 全量 + droppedSources + 可选 focus）。 */
const genOf = (
  entities: AbstractEntity[],
  opts: {
    droppedSources?: number;
    focus?: FocusSummary;
    landings?: GenerateResult['landings'];
  } = {},
): GenerateResult => ({
  ...schemaOf(entities),
  droppedSources: opts.droppedSources ?? 0,
  landings: opts.landings ?? { entities: 0, attributes: 0, error: null },
  focus: opts.focus,
});

const focusOf = (droppedSeeds: number): FocusSummary => ({
  seeds: 2,
  partners: 0,
  neighbors: 1,
  includedTables: 3,
  excludedTables: 4,
  droppedSeeds,
});

// ---------- 驱动 ----------

beforeEach(async () => {
  vi.resetAllMocks();
  // `resetAllMocks` 会清掉 factory 里的默认实现；内嵌的对齐区挂载时就要列一次，
  // 不补回 `{ items: [] }` 它会拿到 undefined（本文件不测该区，空列表即可）。
  listAlignmentsMock.mockResolvedValue({ items: [] });
  getSourcesMock.mockResolvedValue([]);
  // antd 静态 message 的 holder 是一个**独立 React root**，且 antd 首次创建它时把
  // render 包在 act + 微任务链里。若第一次真正的调用发生在测试的 act 作用域内
  // （fireEvent / findBy* 都会包 act），这条「首建渲染」会被排队、toast 永远不进
  // DOM（实测：不预热则 findByText 1000ms 超时也不出现，补一次 act 外调用才刷出）。
  // 所以先在 act 之外把 holder 建起来、再清掉这条预热消息 —— 之后组件发的 toast
  // 都能正常提交。
  message.success('__holder_warmup__');
  await new Promise((r) => setTimeout(r, 150));
  message.destroy();
});

afterEach(() => {
  cleanup();
  message.destroy();
});

async function renderTab() {
  render(<AbstractSchemaTab kbId="kb_1" canWrite />);
  // 初始 load 落地（实体名渲染在 Input 的 value 上，用 displayValue 认）
  await screen.findByDisplayValue('Alpha');
}

async function openFocusModal() {
  fireEvent.click(screen.getByRole('button', { name: 'abstractSchema.regenerate' }));
  await screen.findByText('abstractSchema.focus.title');
  // 此刻 dialog 必须真的在无障碍树里 —— 让 B1 末尾的「已收口」断言不是空转
  expect(screen.getByRole('dialog')).toBeTruthy();
}

/** 聚焦完整两步：填提示词 → 解析 → 种子出现 → 确认生成。 */
async function driveFocusGenerate() {
  fireEvent.change(screen.getByPlaceholderText('abstractSchema.focus.promptPlaceholder'), {
    target: { value: '招聘相关实体' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'abstractSchema.focus.parse' }));
  await screen.findByText('Job'); // 第二步（种子列表）已渲染
  fireEvent.click(screen.getByRole('button', { name: 'abstractSchema.focus.confirm' }));
}

const previewWithJob = {
  seeds: [{ dataSourceId: 'ds_a', entityName: 'Job', sourceName: '招聘库', role: 'focus' as const }],
  neighbors: [],
  warnings: [],
};

describe('AbstractSchemaTab', () => {
  it('B1（T6-2 回归）：生成成功 + 重拉失败 → success 与 refresh 警告并存、无「生成失败」、实体续渲染、弹窗收口', async () => {
    getMock
      .mockResolvedValueOnce(schemaOf([ent('Alpha'), ent('Beta')])) // 挂载时首次 load
      .mockRejectedValueOnce(new Error('network down')); // 生成后的重拉失败
    genMock.mockResolvedValueOnce(genOf([ent('Alpha'), ent('Beta'), ent('Gamma')]));

    await renderTab();
    await openFocusModal();
    fireEvent.click(screen.getByRole('button', { name: 'abstractSchema.focus.skipToFull' }));

    // 1) 成功 toast 照发（重拉失败不撤销「生成已成功」这个事实）
    expect(
      await screen.findByText('abstractSchema.generated({"count":3})'),
    ).toBeTruthy();
    // 2) 额外一条「生成后刷新失败」警告
    expect(await screen.findByText('abstractSchema.refreshAfterGenerateFailed')).toBeTruthy();
    // 3) 绝不出现「生成失败」错误 toast（重拉失败不得落进外层 catch）
    expect(screen.queryByText(/^abstractSchema\.generateFailed/)).toBeNull();
    // 4) 用 generate 回包继续成功路径：新实体仍在页面上
    expect(await screen.findByDisplayValue('Gamma')).toBeTruthy();
    // 5) 弹窗已收口（关闭后不可见于无障碍树）
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('B2a：聚焦生成回包 droppedSeeds=2 → 剔除计数 warning（count=2）+ 聚焦 info 同时出现', async () => {
    getMock.mockResolvedValue(schemaOf([ent('Alpha')]));
    previewMock.mockResolvedValueOnce(previewWithJob);
    genMock.mockResolvedValueOnce(
      genOf([ent('Alpha'), ent('Epsilon')], { focus: focusOf(2) }),
    );

    await renderTab();
    await openFocusModal();
    await driveFocusGenerate();

    // 走的是聚焦路径（而非全量）：seeds 原样提交
    expect(genMock).toHaveBeenCalledWith(
      'kb_1', false,
      [{ dataSourceId: 'ds_a', entityName: 'Job', role: 'focus' }],
      { mode: 'replace', seed: 'structured', autoLandings: false },
    );
    expect(
      await screen.findByText('abstractSchema.focus.droppedSeeds({"count":2})'),
    ).toBeTruthy();
    expect(screen.getByText(/^abstractSchema\.focus\.focused\(/)).toBeTruthy();
  });

  it('B2b：聚焦生成回包 droppedSeeds=0 → 不发剔除计数 toast，聚焦 info 照常', async () => {
    getMock.mockResolvedValue(schemaOf([ent('Alpha')]));
    previewMock.mockResolvedValueOnce(previewWithJob);
    genMock.mockResolvedValueOnce(
      genOf([ent('Alpha'), ent('Epsilon')], { focus: focusOf(0) }),
    );

    await renderTab();
    await openFocusModal();
    await driveFocusGenerate();

    expect(await screen.findByText(/^abstractSchema\.focus\.focused\(/)).toBeTruthy();
    expect(screen.queryByText(/^abstractSchema\.focus\.droppedSeeds/)).toBeNull();
  });

  it('G1：全量生成默认 = 复用文档本体 + 全部重新构造；落点计数回包 → info toast', async () => {
    getMock.mockResolvedValue(schemaOf([ent('Alpha')]));
    genMock.mockResolvedValueOnce(
      genOf([ent('Alpha')], {
        landings: { entities: 2, attributes: 3, error: null },
      }),
    );

    await renderTab();
    await openFocusModal();
    fireEvent.click(screen.getByRole('button', { name: 'abstractSchema.focus.skipToFull' }));

    await waitFor(() => expect(genMock).toHaveBeenCalledWith(
      'kb_1', false, undefined,
      // 弹窗默认：复用文档本体（document）、全部重新构造、不自动落点
      { mode: 'replace', seed: 'document', autoLandings: false },
    ));
    expect(
      await screen.findByText('abstractSchema.landingsDone({"entities":2,"attributes":3})'),
    ).toBeTruthy();
  });

  it('C1：schema active 时审批仍可用（readOnly 仅权限）；生成建议可用；数据源显示名字', async () => {
    getMock.mockResolvedValue(schemaOf([ent('Alpha')], 'active'));
    listAlignmentsMock.mockResolvedValue({ items: [alignmentOf()] });
    getSourcesMock.mockResolvedValue([{ id: 'ds_a', name: '招聘库' }] as any);

    await renderTab();

    // 审批只看写权限：schema 已确认不再锁审批（旧门控会把它一并禁用 → 工作流死锁）
    const activate = await screen.findByRole('button', {
      name: 'domainKnowledge.dataAlignment.activate',
    });
    expect((activate as HTMLButtonElement).disabled).toBe(false);

    // schema active + canWrite ⇒ 生成建议可用（后端只从 active 概念生成）
    const suggest = screen.getByRole('button', {
      name: 'domainKnowledge.dataAlignment.suggest',
    });
    expect((suggest as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText('domainKnowledge.dataAlignment.suggestNeedsActive')).toBeNull();

    // 数据源名替代裸 UUID（title 仍保留完整 id）
    expect(await screen.findByText('招聘库')).toBeTruthy();
    expect(screen.getByText('招聘库').getAttribute('title')).toBe('ds_a');
  });

  it('C2：schema 未确认（proposed）→ 生成建议禁用，原因常显', async () => {
    getMock.mockResolvedValue(schemaOf([ent('Alpha')]));

    render(<AbstractSchemaTab kbId="kb_1" canWrite />);
    await screen.findByDisplayValue('Alpha');

    const suggest = screen.getByRole('button', {
      name: 'domainKnowledge.dataAlignment.suggest',
    });
    expect((suggest as HTMLButtonElement).disabled).toBe(true);
    expect(
      screen.getByText('domainKnowledge.dataAlignment.suggestNeedsActive'),
    ).toBeTruthy();
  });

  it('C3：无抽象 schema → 生成建议禁用，原因在无 schema 分支同样常显', async () => {
    getMock.mockResolvedValue(null);

    render(<AbstractSchemaTab kbId="kb_1" canWrite />);
    await screen.findByText('abstractSchema.empty');

    const suggest = screen.getByRole('button', {
      name: 'domainKnowledge.dataAlignment.suggest',
    });
    expect((suggest as HTMLButtonElement).disabled).toBe(true);
    expect(
      screen.getByText('domainKnowledge.dataAlignment.suggestNeedsActive'),
    ).toBeTruthy();
  });

  it('C4：无写权限 → 审批禁用，提示是权限文案（不混入「先确认 schema」）', async () => {
    getMock.mockResolvedValue(schemaOf([ent('Alpha')], 'active'));
    listAlignmentsMock.mockResolvedValue({ items: [alignmentOf()] });

    render(<AbstractSchemaTab kbId="kb_1" canWrite={false} />);
    await screen.findByText('Employee');

    const activate = screen.getByRole('button', {
      name: 'domainKnowledge.dataAlignment.activate',
    });
    expect((activate as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('domainKnowledge.dataAlignment.readOnlyHint')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.dataAlignment.suggestNeedsActive')).toBeNull();
  });
});
