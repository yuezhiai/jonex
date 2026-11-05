/**
 * 「血缘」页签写入面的组件级回归（jsdom + RTL）。
 *
 * 钉死四件事：
 * - `canWrite` 是**真消费**：false 时新增 / 编辑 / 删除一个按钮都不出现；
 * - 新增载荷的 `data_source_id` **来自页签当前选中的源**（不由用户选），
 *   编辑载荷**不含 `data_source_id`**（边不能换源）；
 * - 删除必须经 `Popconfirm` 二次确认 —— 点删除按钮本身不得发请求；
 * - 后端 400 的 message（「表 X 上没有列 Y」等）**原样透出**，不许吞成通用文案。
 *
 * 断言一律按 i18n key（`react-i18next` 被 mock 成直出键名），不锁中文文案 ——
 * 服务端 message 原文例外（那正是被测对象本身）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { message } from 'antd';

import LineageTab from './LineageTab';
import { getDomainKnowledgeDataSources } from '@/api/domainKnowledge';
import {
  createLineageEdge,
  deleteLineageEdge,
  listLineageEdges,
  updateLineageEdge,
  type LineageEdge,
} from '@/api/lineage';
import { getDataAssets } from '@/api/ontologyDataAssets';
import { getLineageCoverage, getLineageImpact } from '@/api/lineageImpact';
import type { DataSourceConfig } from '@/types/domainKnowledge';

// 恒等的 t（同 FocusGenerateModal.test.tsx 的说明：组件拿它当 effect 依赖）
const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

vi.mock('@/api/domainKnowledge', () => ({
  getDomainKnowledgeDataSources: vi.fn(),
}));

vi.mock('@/api/lineage', () => ({
  listLineageEdges: vi.fn(),
  createLineageEdge: vi.fn(),
  updateLineageEdge: vi.fn(),
  deleteLineageEdge: vi.fn(),
}));

// 画布视图的查询：V1 只验证「全图与血缘边都为空 → 显式空态、不发锚点子图请求」，
// 具体响应形状由图模型测试覆盖。
vi.mock('@/api/ontologyDataAssets', () => ({
  getDataAssets: vi.fn(),
}));

// 常驻覆盖率条与影响分析都走这个模块；不 mock 会真发网络请求（测试慢成分钟级）。
vi.mock('@/api/lineageImpact', () => ({
  getLineageCoverage: vi.fn(),
  getLineageImpact: vi.fn(),
}));

const dsMock = vi.mocked(getDomainKnowledgeDataSources);
const listMock = vi.mocked(listLineageEdges);
const createMock = vi.mocked(createLineageEdge);
const updateMock = vi.mocked(updateLineageEdge);
const deleteMock = vi.mocked(deleteLineageEdge);
const getDataAssetsMock = vi.mocked(getDataAssets);
const getLineageCoverageMock = vi.mocked(getLineageCoverage);
const getLineageImpactMock = vi.mocked(getLineageImpact);

// ---------- 夹具 ----------

function ds(id: string, name: string): DataSourceConfig {
  return {
    id,
    name,
    type: 'MySQL',
    accessType: 'database',
    configJson: {},
    docs: 0,
    status: 'normal',
    desc: '',
    iconType: 'database',
    iconBg: '',
    iconColor: '',
    path: '',
    knowledgeBaseId: 'kb_1',
  };
}

function edge(overrides: Partial<LineageEdge> = {}): LineageEdge {
  return {
    id: 'e1',
    data_source_id: 'ds_a',
    from_table: 'dbo.jobs',
    from_column: 'id',
    to_table: 'dbo.job_forward',
    to_column: 'job_id',
    origin: 'manual',
    note: null,
    ...overrides,
  };
}

// ---------- 驱动 ----------

beforeEach(async () => {
  vi.resetAllMocks();
  dsMock.mockResolvedValue([ds('ds_a', '招聘库')]);
  listMock.mockResolvedValue({ items: [edge()], total: 1 });
  // 画布挂载时会先取整源全图（锚点候选）。默认给空投影：V1 场景仍是显式空态。
  getDataAssetsMock.mockResolvedValue({
    nodes: [],
    edges: [],
    unknown: false,
    truncated: false,
  });
  // 覆盖率条常驻：默认给一个「分母为 0」的空结果，测试不关心它的内容。
  getLineageCoverageMock.mockResolvedValue({
    tables: { total: 0, covered: 0, ratio: null },
    columns: { total: 0, covered: 0, ratio: null },
    by_origin: {},
    orphan_edges: 0,
    total_edges: 0,
    per_table: [],
    truncated: false,
    unknown: false,
  });
  // 影响分析：只有点「分析」才会请求，默认给一个合法空结果（无层级 ≠ 读边失败）。
  getLineageImpactMock.mockResolvedValue({
    anchor: { table: 'dbo.jobs', column: null },
    direction: 'downstream',
    hops: 3,
    levels: [],
    affected: { tables: [], columns: [] },
    total_edges: 0,
    truncated: false,
    unknown: false,
  });
  // antd 静态 message 的 holder 是一个独立 React root，首建渲染若发生在 act 作用域内
  // 会被排队（详见 AbstractSchemaTab.test.tsx 的同款预热注释）。
  message.success('__holder_warmup__');
  await new Promise((r) => setTimeout(r, 150));
  message.destroy();
});

afterEach(() => {
  cleanup();
  message.destroy();
});

async function renderTab(canWrite: boolean) {
  render(<LineageTab kbId="kb_1" canWrite={canWrite} />);
  // 列表落地（端点 chip 是 `表.列` 全串）
  await screen.findByText('dbo.jobs.id');
}

/** 填满四列级必填字段（note 可选，index 4 是 TextArea）。 */
function fillForm(values: { note?: string } = {}) {
  fireEvent.change(screen.getByPlaceholderText('dbo.jobs'), { target: { value: 'dbo.jobs' } });
  fireEvent.change(screen.getByPlaceholderText('id'), { target: { value: 'id' } });
  fireEvent.change(screen.getByPlaceholderText('dbo.job_forward'), {
    target: { value: 'dbo.job_forward' },
  });
  fireEvent.change(screen.getByPlaceholderText('job_id'), { target: { value: 'job_id' } });
  if (values.note) {
    fireEvent.change(screen.getAllByRole('textbox')[4], { target: { value: values.note } });
  }
}

// ---------- 用例 ----------

describe('LineageTab 写入面', () => {
  it('W1：canWrite=false → 新增 / 编辑 / 删除都不渲染（权限不是靠后端拦）', async () => {
    await renderTab(false);

    expect(screen.queryByRole('button', { name: /domainKnowledge\.lineage\.addEdge/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /common\.edit/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /common\.delete/ })).toBeNull();
    // 只读提示在场、可写提示不在场
    expect(screen.getByText('domainKnowledge.lineage.readonlyHint')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.writableHint')).toBeNull();
  });

  it('W2：canWrite=true → 新增时 data_source_id 取当前源，成功后 reload', async () => {
    createMock.mockResolvedValueOnce(edge({ id: 'e2' }));
    await renderTab(true);

    fireEvent.click(screen.getByRole('button', { name: /domainKnowledge\.lineage\.addEdge/ }));
    await screen.findByText('domainKnowledge.lineage.addEdgeTitle');
    fillForm({ note: '手工补录入' });
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1));
    expect(createMock).toHaveBeenCalledWith('kb_1', {
      from_table: 'dbo.jobs',
      from_column: 'id',
      to_table: 'dbo.job_forward',
      to_column: 'job_id',
      note: '手工补录入',
      data_source_id: 'ds_a',
    });
    // 成功后刷新列表（首次加载 + 写入后 reload）
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
  });

  it('W3：新增失败 → 后端 message 原样透出，不落通用文案，弹窗保持打开', async () => {
    const serverMessage = '表 dbo.jobs 上没有列 nope（端点 from_column）';
    createMock.mockRejectedValueOnce(new Error(serverMessage));
    await renderTab(true);

    fireEvent.click(screen.getByRole('button', { name: /domainKnowledge\.lineage\.addEdge/ }));
    await screen.findByText('domainKnowledge.lineage.addEdgeTitle');
    fillForm();
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));

    expect(await screen.findByText(serverMessage)).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.createFailed')).toBeNull();
    // 失败不关弹窗：用户改完还能再提交
    expect(screen.getByText('domainKnowledge.lineage.addEdgeTitle')).toBeTruthy();
    expect(listMock).toHaveBeenCalledTimes(1); // 只有首次加载，没有 reload
  });

  it('W4：编辑 → 预填现有值，载荷不含 data_source_id（边不能换源）', async () => {
    updateMock.mockResolvedValueOnce(edge({ note: '改过的备注' }));
    await renderTab(true);

    fireEvent.click(screen.getByRole('button', { name: /common\.edit/ }));
    await screen.findByText('domainKnowledge.lineage.editEdgeTitle');

    expect((screen.getByPlaceholderText('dbo.jobs') as HTMLInputElement).value).toBe('dbo.jobs');
    expect((screen.getByPlaceholderText('id') as HTMLInputElement).value).toBe('id');
    expect((screen.getByPlaceholderText('dbo.job_forward') as HTMLInputElement).value).toBe(
      'dbo.job_forward',
    );
    expect((screen.getByPlaceholderText('job_id') as HTMLInputElement).value).toBe('job_id');

    fillForm();
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    const [kbArg, idArg, body] = updateMock.mock.calls[0];
    expect(kbArg).toBe('kb_1');
    expect(idArg).toBe('e1');
    expect(body).not.toHaveProperty('data_source_id');
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
  });

  it('W5：删除必须经 Popconfirm 二次确认 —— 点删除按钮本身不发请求', async () => {
    deleteMock.mockResolvedValueOnce(true);
    await renderTab(true);

    fireEvent.click(screen.getByRole('button', { name: /common\.delete/ }));
    // Popconfirm 文案出现之前/之后，未点确认都不得发请求
    expect(await screen.findByText('domainKnowledge.lineage.deleteConfirm')).toBeTruthy();
    expect(deleteMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'common.confirm' }));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith('kb_1', 'e1'));
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2));
  });

  it('W6：删除失败 → 后端 message 原样透出（如「血缘边不存在」），不落通用文案', async () => {
    const serverMessage = '血缘边不存在';
    deleteMock.mockRejectedValueOnce(new Error(serverMessage));
    await renderTab(true);

    fireEvent.click(screen.getByRole('button', { name: /common\.delete/ }));
    await screen.findByText('domainKnowledge.lineage.deleteConfirm');
    fireEvent.click(screen.getByRole('button', { name: 'common.confirm' }));

    expect(await screen.findByText(serverMessage)).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.deleteFailed')).toBeNull();
    expect(listMock).toHaveBeenCalledTimes(1); // 失败不刷新
  });
});

describe('LineageTab 列表/画布视图切换', () => {
  it('S1：stale=true 的边显示「已失效」标记；非 stale 的边不显示', async () => {
    listMock.mockResolvedValue({
      items: [
        edge({ id: 'e_stale', origin: 'platform', stale: true }),
        edge({
          id: 'e_fresh', origin: 'platform', stale: false,
          from_table: 'dbo.company', to_table: 'dbo.jobs',
          from_column: 'id', to_column: 'company_id',
        }),
      ],
      total: 2,
    });
    render(<LineageTab kbId="kb_1" canWrite={false} />);

    expect(await screen.findByText('domainKnowledge.lineage.stale')).toBeTruthy();
    // 只有一条边过期：标记也必须只有一处
    expect(screen.getAllByText('domainKnowledge.lineage.stale')).toHaveLength(1);
    // 悬停说明走 title（不占列宽）
    expect(screen.getByTitle('domainKnowledge.lineage.staleHint')).toBeTruthy();
  });

  it('V1：切到画布且全图/血缘边都为空 → 显式「没有可用数据资产表」，不发锚点子图请求；切回列表还原', async () => {
    listMock.mockResolvedValue({ items: [], total: 0 });
    render(<LineageTab kbId="kb_1" canWrite={false} />);
    // 列表先落地（整源空态）
    await screen.findByText('domainKnowledge.lineage.empty');

    fireEvent.click(screen.getByText('domainKnowledge.lineage.viewGraph'));

    // 无锚点候选：画布给显式空态（不是空画布，也不是「无上下游」）
    expect(await screen.findByText('domainKnowledge.lineage.graph.noTables')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.empty')).toBeNull();
    // 画布现在会先取整源全图（锚点候选，不带 table）；全图也为空 ⇒ 不发锚点子图请求。
    expect(getDataAssetsMock).toHaveBeenCalledTimes(1);
    expect(getDataAssetsMock).toHaveBeenCalledWith('kb_1', 'ds_a', {
      direction: 'both',
      hops: 1,
    });

    // 切回列表：列表空态回来（列表行为未被视图切换改写）
    fireEvent.click(screen.getByText('domainKnowledge.lineage.viewList'));
    expect(await screen.findByText('domainKnowledge.lineage.empty')).toBeTruthy();
    expect(screen.queryByText('domainKnowledge.lineage.graph.noTables')).toBeNull();
  });
});

describe('LineageTab 覆盖率条与影响分析接线', () => {
  it('C1：边首载成功后才按 (kbId, dsId) 请求覆盖率 —— 同一个源只请求一次', async () => {
    await renderTab(false);

    // 覆盖率条在边落地后挂载
    await screen.findByText('domainKnowledge.lineage.coverage.title');
    expect(getLineageCoverageMock).toHaveBeenCalledTimes(1);
    expect(getLineageCoverageMock).toHaveBeenCalledWith('kb_1', 'ds_a');
  });

  it('C2：切到影响分析 → 显示 idle 文案，分析请求带当前 (kbId, dsId)', async () => {
    await renderTab(false);

    fireEvent.click(screen.getByText('domainKnowledge.lineage.viewImpact'));
    // 锚点候选来自当前源的边集；未点「分析」前是 idle，不是空结果
    expect(await screen.findByText('domainKnowledge.lineage.impact.idle')).toBeTruthy();

    fireEvent.click(
      screen.getByRole('button', { name: /domainKnowledge\.lineage\.impact\.analyze/ }),
    );
    await waitFor(() => expect(getLineageImpactMock).toHaveBeenCalledTimes(1));
    const [kbArg, dsArg] = getLineageImpactMock.mock.calls[0];
    expect(kbArg).toBe('kb_1');
    expect(dsArg).toBe('ds_a');
  });
});


describe('LineageTab 固定数据源（从数据源卡片进入）', () => {
  it('只加载固定源并隐藏数据源选择器', async () => {
    dsMock.mockResolvedValue([ds('ds_a', '招聘库'), ds('ds_b', '订单库')]);
    listMock.mockResolvedValue({ items: [], total: 0 });

    render(<LineageTab kbId="kb_1" fixedDataSourceId="ds_b" />);

    await waitFor(() => expect(listMock).toHaveBeenCalledWith('kb_1', 'ds_b'));
    expect(screen.queryByText('domainKnowledge.lineage.dataSource')).toBeNull();
  });
});
