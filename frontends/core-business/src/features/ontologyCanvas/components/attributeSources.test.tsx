/**
 * 字段落点编辑器组件测试（jsdom + RTL）：行渲染 / 删除 / role 行内编辑 / 添加流程
 * （候选约束 = 只列 included 落点）/ readOnly 闸。断言按 i18n key，不锁中文文案。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import { AttributeSourcesBlock } from './attributeSources';
import type { Attribute, EntityType } from '../domain/index.js';

const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) =>
    (params ? `${key}(${JSON.stringify(params)})` : key),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: tMock }) }));

const getAllCompiledSchema = vi.hoisted(() => vi.fn());
vi.mock('../multiSource/load.js', () => ({
  getCompiledSchema: getAllCompiledSchema,
}));

const getAllSynonyms = vi.hoisted(() => vi.fn());
vi.mock('@/api/ontologySynonym', () => ({
  exportAllSynonyms: getAllSynonyms,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  getAllSynonyms.mockResolvedValue([]);   // 缺省：空同义词表（= 不接桥）
});

function makeEntity(): EntityType {
  return {
    id: 'e:地址', name: '地址', aliases: [], landings: [
      { dataSourceId: 'ds_a', entityName: 'Company', included: true, sourceName: '锦聘', stale: false },
      { dataSourceId: 'ds_b', entityName: 'Addr', included: false, sourceName: '下线源', stale: false },
    ],
    attributes: [],
  };
}

function makeAttr(sources: Attribute['sources'], name = '地址'): Attribute {
  return { id: `a:${name}.${name}`, name, type: 'string', required: false,
           primaryKey: false, sources };
}

function renderBlock(attribute: Attribute, readOnly = false) {
  const execute = vi.fn();
  render(<AttributeSourcesBlock kbId="kb_1" entity={makeEntity()} attribute={attribute}
                                execute={execute} readOnly={readOnly} />);
  return { execute };
}

describe('AttributeSourcesBlock', () => {
  it('删除一条来源 → attribute.update 过滤后的 sources', () => {
    const { execute } = renderBlock(makeAttr([
      { dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'city', role: '市' },
      { dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'province', role: '省' },
    ]));
    fireEvent.click(screen.getAllByText('common.delete')[0]!);
    expect(execute).toHaveBeenCalledTimes(1);
    const [command] = execute.mock.calls[0]!;
    expect(command).toMatchObject({
      type: 'attribute.update', entityId: 'e:地址', attributeId: 'a:地址.地址',
      changes: { sources: [{ dataSourceId: 'ds_a', entityName: 'Company',
                             attributeName: 'province', role: '省' }] },
    });
  });

  it('行内编辑 role → 首尾空白 trim 后入库；清空 → role 归 undefined', () => {
    const { execute } = renderBlock(makeAttr([
      { dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'city', role: '市' },
    ]));
    const input = screen.getByDisplayValue('市');
    // 取「最后一次调用」用下标而不是 `mock.calls.at(-1)`：本仓 tsconfig target=ES2020，
    // `Array.prototype.at` 要 ES2022 lib，`tsc --noEmit` 会直接报 TS2550。
    const lastSources = () =>
      (execute.mock.calls[execute.mock.calls.length - 1]![0] as any).changes.sources;
    fireEvent.change(input, { target: { value: ' 城市 ' } });
    let sources = lastSources();
    expect(sources[0].role).toBe('城市');
    fireEvent.change(input, { target: { value: '' } });
    sources = lastSources();
    expect(sources[0].role).toBeUndefined();
  });

  it('编辑 role 不丢其它语义槽', () => {
    const { execute } = renderBlock(makeAttr([
      { dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'gen',
        role: '旧', filter: 'gen > 0', aggregation: 'SUM',
        grain: 'per day', enabled: false },
    ]));
    const input = screen.getByDisplayValue('旧');
    fireEvent.change(input, { target: { value: '新' } });
    // 本仓 tsconfig target=ES2020：不要用 `mock.calls.at(-1)`（TS2550）
    const lastSources = () =>
      (execute.mock.calls[execute.mock.calls.length - 1]![0] as any).changes.sources;
    expect(lastSources()[0]).toMatchObject({
      role: '新', filter: 'gen > 0', aggregation: 'SUM',
      grain: 'per day', enabled: false,
    });
  });

  it('添加流程：只列 included 落点 → 选源/实体/列/role → 确认', async () => {
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [{ name: 'city' }, { name: 'province' }] }],
    });
    const { execute } = renderBlock(makeAttr([]));

    fireEvent.click(screen.getByText('attributeSources.add'));
    expect(screen.getByText('attributeSources.pickerTitle')).toBeTruthy();

    // 数据源下拉：只有 included 的 ds_a（ds_b 是 excluded 落点，不出现）
    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]!);
    fireEvent.click(await screen.findByTitle('锦聘'));
    expect(screen.queryByTitle('下线源')).toBeNull();

    // 实体 → 拉编译 schema；列
    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]!);
    fireEvent.click(await screen.findByTitle('Company'));
    expect(getAllCompiledSchema).toHaveBeenCalledWith('kb_1', 'ds_a');
    fireEvent.mouseDown(screen.getAllByRole('combobox')[2]!);
    fireEvent.click(await screen.findByTitle('city'));

    fireEvent.change(screen.getByPlaceholderText('attributeSources.rolePlaceholder'),
                     { target: { value: '市' } });
    fireEvent.click(screen.getByText('attributeSources.confirm'));

    const calls = execute.mock.calls;
    const [command] = calls[calls.length - 1]!;
    expect(command).toMatchObject({
      type: 'attribute.update',
      changes: { sources: [{ dataSourceId: 'ds_a', entityName: 'Company',
                             attributeName: 'city', role: '市' }] },
    });
  });

  it('添加流程：选中已存在的三元组 → 显示 duplicated 提示、确认按钮禁用', async () => {
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [{ name: 'city' }, { name: 'province' }] }],
    });
    renderBlock(makeAttr([
      { dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'city', role: '市' },
    ]));

    fireEvent.click(screen.getByText('attributeSources.add'));
    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]!);
    fireEvent.click(await screen.findByTitle('锦聘'));
    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]!);
    fireEvent.click(await screen.findByTitle('Company'));
    expect(screen.queryByText('attributeSources.duplicated')).toBeNull();

    fireEvent.mouseDown(screen.getAllByRole('combobox')[2]!);
    fireEvent.click(await screen.findByTitle('city'));
    expect(screen.getByText('attributeSources.duplicated')).toBeTruthy();
    const confirm = screen.getByText('attributeSources.confirm').closest('button')!;
    expect(confirm.disabled).toBe(true);
  });

  it('添加流程：源实体没有可选列 → 显示 noColumn 提示', async () => {
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [] }],
    });
    renderBlock(makeAttr([]));

    fireEvent.click(screen.getByText('attributeSources.add'));
    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]!);
    fireEvent.click(await screen.findByTitle('锦聘'));
    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]!);
    fireEvent.click(await screen.findByTitle('Company'));

    expect(await screen.findByText('attributeSources.noColumn')).toBeTruthy();
  });

  it('readOnly：删除按钮禁用、不渲染空态块', () => {
    renderBlock(makeAttr([
      { dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'city' },
    ]), true);
    const del = screen.getByText('common.delete').closest('button')!;
    expect(del.disabled).toBe(true);

    cleanup();
    renderBlock(makeAttr([]), true);
    expect(screen.queryByText('attributeSources.add')).toBeNull();  // 只读 + 空 = 不渲染
  });

  it('建议流程：命中同名列 → 勾选 → 追加落点（role 不预填）', async () => {
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [
        { name: '地址' }, { name: 'note' }, { name: 'address_city', display_name: '地址' },
      ] }],
    });
    const { execute } = renderBlock(makeAttr([]));

    fireEvent.click(screen.getByText('attributeSources.suggest'));
    expect(await screen.findByText('attributeSources.suggestTitle')).toBeTruthy();
    expect(getAllCompiledSchema).toHaveBeenCalledWith('kb_1', 'ds_a');

    // 属性名「地址」：列 `地址` 命中 exact；`address_city` 命中 display（显示名「地址」）；`note` 不命中
    expect(screen.queryByText('note')).toBeNull();
    const boxes = screen.getAllByRole('checkbox');
    expect(boxes).toHaveLength(2);
    fireEvent.click(boxes[0]!);
    fireEvent.click(boxes[1]!);

    fireEvent.click(screen.getByRole('button', { name: /attributeSources\.suggestAdd/ }));

    const calls = execute.mock.calls;
    const [command] = calls[calls.length - 1]!;
    expect(command).toMatchObject({
      type: 'attribute.update', entityId: 'e:地址', attributeId: 'a:地址.地址',
      changes: { sources: [
        { dataSourceId: 'ds_a', entityName: 'Company', attributeName: '地址' },
        { dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'address_city' },
      ] },
    });
    // role 不预填
    expect((command as any).changes.sources[0].role).toBeUndefined();
    // 落点只含三元组：hit（展示档）不进落点
    expect((command as any).changes.sources[0].hit).toBeUndefined();
    expect((command as any).changes.sources[1].hit).toBeUndefined();
    expect((command as any).changes.sources[1].role).toBeUndefined();
    // 同一 ds 只拉一次
    expect(getAllCompiledSchema).toHaveBeenCalledTimes(1);
  });

  it('建议流程：无候选 → 显示 suggestEmpty', async () => {
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [{ name: 'note' }] }],
    });
    renderBlock(makeAttr([]));
    fireEvent.click(screen.getByText('attributeSources.suggest'));
    expect(await screen.findByText('attributeSources.suggestEmpty')).toBeTruthy();
  });

  it('面板级缓存：两个属性共用同一个 schemaCache → 同一 ds 只拉一次', async () => {
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [{ name: '城市' }] }],
    });
    const schemaCache = { current: new Map() } as any;
    render(<AttributeSourcesBlock kbId="kb_1" entity={makeEntity()} attribute={makeAttr([])}
                                   execute={vi.fn()} readOnly={false} schemaCache={schemaCache} />);
    render(<AttributeSourcesBlock kbId="kb_1" entity={makeEntity()} attribute={makeAttr([])}
                                   execute={vi.fn()} readOnly={false} schemaCache={schemaCache} />);
    const suggests = screen.getAllByText('attributeSources.suggest');
    fireEvent.click(suggests[0]!);
    await screen.findAllByText('attributeSources.suggestTitle');
    fireEvent.click(suggests[1]!);
    await screen.findAllByText('attributeSources.suggestTitle');
    expect(getAllCompiledSchema).toHaveBeenCalledTimes(1);
  });

  it('建议取数失败 → 显示错误态与重试，且不显示 suggestEmpty', async () => {
    getAllCompiledSchema.mockRejectedValue(new Error('boom'));
    renderBlock(makeAttr([]));

    fireEvent.click(screen.getByText('attributeSources.suggest'));
    expect(await screen.findByText(/attributeSources\.suggestError/)).toBeTruthy();
    expect(screen.getByText('attributeSources.suggestRetry')).toBeTruthy();
    // 「取不到」与「没有候选」是两回事，不能显示成后者
    expect(screen.queryByText('attributeSources.suggestEmpty')).toBeNull();
  });

  it('建议取数：null（该源未编译）不进缓存 → 再点建议仍会重新拉', async () => {
    getAllCompiledSchema.mockResolvedValue(null);
    renderBlock(makeAttr([]));

    fireEvent.click(screen.getByText('attributeSources.suggest'));
    await screen.findByText('attributeSources.suggestEmpty');
    expect(getAllCompiledSchema).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('attributeSources.suggest'));
    // 缓存了 null 的实现只有 1 次；不缓存 → 2 次
    await waitFor(() => expect(getAllCompiledSchema).toHaveBeenCalledTimes(2));
  });

  it('synonym：同义词组桥接 → 命中 synonym 档并可勾选追加', async () => {
    getAllSynonyms.mockResolvedValue([{ id: 's1', terms: ['职位', 'job_title'], canonical: '职位' }]);
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [{ name: 'job_title' }, { name: 'note' }] }],
    });
    const { execute } = renderBlock(makeAttr([], '职位'));

    fireEvent.click(screen.getByText('attributeSources.suggest'));
    expect(await screen.findByText('attributeSources.hitSynonym')).toBeTruthy();
    expect(screen.queryByText('note')).toBeNull();

    fireEvent.click(screen.getAllByRole('checkbox')[0]!);
    fireEvent.click(screen.getByRole('button', { name: /attributeSources\.suggestAdd/ }));

    const calls = execute.mock.calls;
    const [command] = calls[calls.length - 1]!;
    expect(command).toMatchObject({
      type: 'attribute.update',
      changes: { sources: [{ dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'job_title' }] },
    });
    expect(getAllSynonyms).toHaveBeenCalledWith('kb_1');
  });

  it('synonym：同义词表取不到 → 降级提示 + 仍按名字出候选', async () => {
    getAllSynonyms.mockRejectedValue(new Error('synonym down'));
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [{ name: '地址' }] }],
    });
    renderBlock(makeAttr([]));

    fireEvent.click(screen.getByText('attributeSources.suggest'));
    expect(await screen.findByText('attributeSources.suggestFallback')).toBeTruthy();
    // 降级不阻塞：名字命中的候选照常出现
    expect(screen.getByText('地址')).toBeTruthy();
  });

  it('synonym：同义词表取不到**且**零候选 → 降级提示照样可见（不能只剩 suggestEmpty）', async () => {
    getAllSynonyms.mockRejectedValue(new Error('synonym down'));
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [{ name: 'note' }] }],
    });
    renderBlock(makeAttr([]));

    fireEvent.click(screen.getByText('attributeSources.suggest'));
    // 提示必须在弹窗顶部、状态分支之外 —— 这条用例就是它位置的守卫
    expect(await screen.findByText('attributeSources.suggestFallback')).toBeTruthy();
    expect(screen.getByText('attributeSources.suggestEmpty')).toBeTruthy();
  });
});

describe('AttributeSourcesBlock 来源提示', () => {
  it('有来源时显示「由生成/对齐产生」提示', () => {
    renderBlock(makeAttr([
      { dataSourceId: 'ds_a', entityName: 'Company', attributeName: 'city' },
    ]));
    expect(screen.getByText('attributeSources.originHint')).toBeTruthy();
  });

  it('没有来源时不显示提示', () => {
    renderBlock(makeAttr([]));
    expect(screen.queryByText('attributeSources.originHint')).toBeNull();
  });
});
