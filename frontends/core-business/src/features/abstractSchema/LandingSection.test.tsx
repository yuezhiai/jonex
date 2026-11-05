/**
 * 实体落点面板的「建议落点」（跨源一键建议）组件测试（jsdom + RTL）：
 * 分组渲染 / 弱候选默认折叠 / 勾选写 entity.update 实参 / 取数失败错误态。
 * 断言按 i18n key，不锁中文文案。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';

import { LandingSection } from './LandingSection';
import type { EntityType } from '../ontologyCanvas/domain/index.js';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    (params ? `${key}(${JSON.stringify(params)})` : key),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));

const listAllDataSources = vi.hoisted(() => vi.fn());
vi.mock('@/api/dataSource', () => ({ listDataSources: listAllDataSources }));

const getAllCompiledSchema = vi.hoisted(() => vi.fn());
vi.mock('../ontologyCanvas/multiSource/load.js', () => ({ getCompiledSchema: getAllCompiledSchema }));

const getAllSynonyms = vi.hoisted(() => vi.fn());
vi.mock('@/api/ontologySynonym', () => ({ exportAllSynonyms: getAllSynonyms }));

function makeEntity(): EntityType {
  return { id: 'e:job_title', name: 'job_title', aliases: [], attributes: [], landings: [] };
}

function renderSection(entity = makeEntity(), readOnly = false) {
  const execute = vi.fn();
  render(<LandingSection kbId="kb_1" entity={entity} entityNames={['job_title']}
                         execute={execute} readOnly={readOnly} />);
  return { execute };
}

async function openSuggest() {
  fireEvent.click(screen.getByText('abstractEditor.suggestLandings'));
  await screen.findAllByText('abstractEditor.suggestTitle');
}

/** 建议弹窗的 DOM 根 —— 落点行本身也渲染 `<code>实体名</code>` 与 antd Checkbox，
 *  所有候选相关断言**必须限定在弹窗内**，否则会跟落点行互相干扰。 */
function suggestDialog(): HTMLElement {
  return screen.getByText('abstractEditor.suggestTitle').closest('.ant-modal') as HTMLElement;
}

// ⚠️ 夹具的抽象实体名取 `job_title`（**≥3 个字符**）：`token` 档要求短串长度 ≥3，
// 用「职位」这类 2 字中文名做夹具的话 token 档恒不可达，弱候选相关用例会全空转。
beforeEach(() => {
  listAllDataSources.mockResolvedValue([
    { id: 'ds_a', name: 'genesis', accessType: 'database' },
    { id: 'ds_b', name: '锦聘', accessType: 'database' },
  ]);
  getAllCompiledSchema.mockImplementation(async (_kb: string, dsId: string) => ({
    entity_types: dsId === 'ds_a'
      ? [{ name: 'job_title' }, { name: 'job' }, { name: 'note' }]
      : [{ name: 'job_title' }],
  }));
  getAllSynonyms.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('LandingSection 建议落点', () => {
  it('跨源汇总并按源分组：强档列出、弱档折叠在 weakMore 行', async () => {
    const { execute } = renderSection();
    await openSuggest();
    const dlg = suggestDialog();

    // 两个组头（ds_a / ds_b 各一）
    expect(within(dlg).getAllByText(/abstractEditor\.suggestGroupHead/)).toHaveLength(2);
    // 强档 job_title 在两组各一条
    expect(within(dlg).getAllByText('job_title')).toHaveLength(2);
    // 弱档 job（ds_a）折叠中，未展开不可见；ds_b 没有弱候选 → 只有一行 weakMore
    expect(within(dlg).queryByText('job')).toBeNull();
    expect(within(dlg).getAllByText(/abstractEditor\.suggestWeakMore/)).toHaveLength(1);
    expect(execute).not.toHaveBeenCalled();
  });

  it('展开弱候选 → 弱档可见', async () => {
    renderSection();
    await openSuggest();
    fireEvent.click(within(suggestDialog()).getByText(/abstractEditor\.suggestWeakMore/));
    expect(await within(suggestDialog()).findByText('job')).toBeTruthy();
  });

  it('勾选 → 一次 entity.update，追加 included:true 的落点（含 sourceName/stale 归一）', async () => {
    const { execute } = renderSection();
    await openSuggest();
    const dlg = suggestDialog();

    // 组按源 id 排序 → ds_a 在前；组内第一个强档即 ds_a/job_title
    fireEvent.click(within(dlg).getAllByRole('checkbox')[0]!);
    fireEvent.click(within(dlg).getByRole('button', { name: /abstractEditor\.suggestAdd/ }));

    const calls = execute.mock.calls;
    const [command] = calls[calls.length - 1]!;
    expect(command).toMatchObject({
      type: 'entity.update', entityId: 'e:job_title',
      changes: { landings: [{
        dataSourceId: 'ds_a', entityName: 'job_title', included: true,
        sourceName: 'genesis', stale: false, staleReason: null,
      }] },
    });
  });

  it('已登记落点不再出现在候选里（含 included:false 的也排除）', async () => {
    const withLanding: EntityType = {
      ...makeEntity(),
      landings: [{ dataSourceId: 'ds_a', entityName: 'job_title', included: false, stale: false }],
    };
    renderSection(withLanding);
    await openSuggest();
    const dlg = suggestDialog();

    // ds_a 的 job_title 被排除 → 弹窗里只剩 ds_b 那一条强档
    // （注意不在整页查：落点行自己也会渲染 `<code>job_title</code>`）
    expect(within(dlg).getAllByText('job_title')).toHaveLength(1);
  });

  it('取数失败 → 错误态与重试，不显示 suggestEmpty', async () => {
    listAllDataSources.mockRejectedValue(new Error('boom'));
    renderSection();
    fireEvent.click(screen.getByText('abstractEditor.suggestLandings'));
    expect(await screen.findByText(/abstractEditor\.suggestError/)).toBeTruthy();
    expect(screen.getByText('abstractEditor.suggestRetry')).toBeTruthy();
    expect(screen.queryByText('abstractEditor.suggestEmpty')).toBeNull();
  });

  it('同义词表取不到 → 降级提示 + 候选仍按名字出（降级不阻塞）', async () => {
    getAllSynonyms.mockRejectedValue(new Error('synonym down'));
    renderSection();
    await openSuggest();
    const dlg = suggestDialog();

    expect(within(dlg).getByText('abstractEditor.suggestFallback')).toBeTruthy();
    // 降级后名字命中的候选照常出（ds_a / ds_b 各一条 job_title）
    expect(within(dlg).getAllByText('job_title')).toHaveLength(2);
  });

  it('同义词表取不到 **且** 零候选 → 降级提示照样可见（不能只剩 suggestEmpty 让用户猜）', async () => {
    getAllSynonyms.mockRejectedValue(new Error('synonym down'));
    getAllCompiledSchema.mockResolvedValue({ entity_types: [{ name: 'note' }] });  // 无任何命中
    renderSection();
    await openSuggest();
    const dlg = suggestDialog();

    // 提示在弹窗顶部、三种状态之外 —— 这条用例是它位置的守卫
    expect(within(dlg).getByText('abstractEditor.suggestFallback')).toBeTruthy();
    expect(within(dlg).getByText('abstractEditor.suggestEmpty')).toBeTruthy();
  });

  it('部分源取不到 **且** 零候选 → suggestPartial 照样可见（与降级提示同位）', async () => {
    // ds_a 的 schema 取不到（部分失败）、ds_b 正常但零命中
    getAllCompiledSchema.mockImplementation(async (_kb: string, dsId: string) => {
      if (dsId === 'ds_a') throw new Error('ds_a down');
      return { entity_types: [{ name: 'note' }] };
    });
    renderSection();
    await openSuggest();
    const dlg = suggestDialog();

    // 提示在弹窗顶部、三种状态之外 —— 这条用例是它位置的守卫
    expect(within(dlg).getByText(/abstractEditor\.suggestPartial/)).toBeTruthy();
    expect(within(dlg).getByText('abstractEditor.suggestEmpty')).toBeTruthy();
  });

  it('只有弱候选的源 → 组头不出现空计数（回落到 weak 计数）', async () => {
    // 抽象实体名 job_title，候选名 job → 只命中 token（弱档），strong 为空
    getAllCompiledSchema.mockResolvedValue({ entity_types: [{ name: 'job' }] });
    renderSection();
    await openSuggest();
    const dlg = suggestDialog();

    const heads = within(dlg).getAllByText(/abstractEditor\.suggestGroupHead/);
    expect(heads).toHaveLength(2);
    // 计数不能是空串 —— 空串的组头渲染出来不含任何命中档名
    heads.forEach((h) => expect(h.textContent).toContain('hitToken'));
  });

  it('readOnly：建议落点按钮禁用', () => {
    renderSection(makeEntity(), true);
    const btn = screen.getByText('abstractEditor.suggestLandings').closest('button')!;
    expect(btn.disabled).toBe(true);
  });
});

describe('LandingSection 落点行视觉', () => {
  it('included=false 的落点行整行淡化，但「打开源画布」链接保持可点观感', () => {
    const entity: EntityType = {
      ...makeEntity(),
      landings: [{
        dataSourceId: 'ds_a', entityName: 'job_title', included: false,
        sourceName: 'genesis', stale: false,
      }],
    };
    renderSection(entity);

    const openButton = screen.getByText('abstractEditor.openSourceCanvas').closest('button')!;
    const row = openButton.closest('.ae-landing')!;
    expect(row.className).toContain('is-off');
    // 链接自身不再随行淡化（CSS 只对非链接子元素降透明度）
    expect(openButton.className).toContain('ae-landing-open');
  });
});
