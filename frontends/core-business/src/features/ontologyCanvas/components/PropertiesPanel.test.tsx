/**
 * 规则面板组件级回归（jsdom + RTL）：列表渲染/选中、加规则会落到合法目标、
 * query_rule 编辑面渲染与 onChange、severity 行隐藏、抽象态不渲染规则段，
 * 关系段列表/加规则/重复警告（含关系 Select 改选闸：占用拒绝 + 自由放行）。
 * 断言按 i18n key（react-i18next 被 mock 成直出键名），不锁中文文案。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { message } from 'antd';

import { PropertiesPanel } from './PropertiesPanel';
import type { Constraint, DomainDocument, EntityType, RelationType } from '../domain/index.js';

// useTranslation 必须返回逐次渲染恒等的 t（组件把它用进 handler/依赖），否则 effect 无限重跑。
const { tMock } = vi.hoisted(() => ({
  tMock: (key: string, params?: Record<string, unknown>) => (params ? `${key}(${JSON.stringify(params)})` : key),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

const getAllCompiledSchema = vi.hoisted(() => vi.fn());
vi.mock('../multiSource/load.js', () => ({
  getCompiledSchema: getAllCompiledSchema,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function makeDocument(
  extraConstraints: Constraint[] = [],
  extraEntities: EntityType[] = [],
  extraRelations: RelationType[] = [],
): DomainDocument {
  return {
    storageVersion: 1,
    schemaVersion: 1,
    revision: 0,
    domain: { id: 'kb_1', displayName: 'kb_1' },
    model: {
      entities: [
        {
          id: 'e:Jobs', name: 'Jobs', aliases: [],
          attributes: [
            { id: 'a:Jobs.status', name: 'status', type: 'string', required: false, primaryKey: false },
            { id: 'a:Jobs.fee_unit', name: 'fee_unit', type: 'string', required: false, primaryKey: false },
          ],
        },
        ...extraEntities,
      ],
      relations: [
        { id: 'r:OWNS', name: 'OWNS', sourceEntityId: 'e:Jobs', targetEntityId: 'e:Jobs' },
        ...extraRelations,
      ],
      constraints: [
        { id: 'c:query_rule:Jobs.status', type: 'query_rule', entityId: 'e:Jobs', attributeId: 'a:Jobs.status', severity: 'error', expression: '口径原文' },
        ...extraConstraints,
      ],
      disambiguation: { caseInsensitive: true, aliasMerge: true },
    },
    editor: { nodePositions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
    savedAt: '2026-10-07T00:00:00Z',
  };
}

function renderPanel(
  selection: { kind: 'entity' | 'relation' | 'constraint'; id: string },
  fieldSet?: 'compiled' | 'abstract',
  extraConstraints: Constraint[] = [],
  extraEntities: EntityType[] = [],
  extraRelations: RelationType[] = [],
  kbId?: string,
) {
  const document = makeDocument(extraConstraints, extraEntities, extraRelations);
  const execute = vi.fn();
  const select = vi.fn();
  render(
    <PropertiesPanel
      document={document}
      selection={selection}
      execute={execute}
      addAttribute={vi.fn()}
      select={select}
      {...(fieldSet ? { fieldSet } : {})}
      {...(kbId ? { kbId } : {})}
    />,
  );
  return { execute, select };
}

describe('PropertiesPanel 规则面板', () => {
  it('实体选中：列出该实体的规则，点击 → select({kind:"constraint"})', () => {
    const { select } = renderPanel({ kind: 'entity', id: 'e:Jobs' });

    expect(screen.getByText('rule.listTitle')).toBeTruthy();
    fireEvent.click(screen.getByText('rule.query_rule'));
    expect(select).toHaveBeenCalledWith({ kind: 'constraint', id: 'c:query_rule:Jobs.status' });
  });

  it('加规则：点击下拉选 query_rule → constraint.add 落到未占用的合法目标并选中', async () => {
    const { execute } = renderPanel({ kind: 'entity', id: 'e:Jobs' });

    fireEvent.click(screen.getByRole('button', { name: 'rule.add' }));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'rule.query_rule' }));

    expect(execute).toHaveBeenCalledTimes(1);
    const [command, selection] = execute.mock.calls[0]!;
    expect(command.type).toBe('constraint.add');
    expect((command as { constraint: Constraint }).constraint).toMatchObject({
      type: 'query_rule', entityId: 'e:Jobs', attributeId: 'a:Jobs.fee_unit', expression: '', severity: 'error',
    });
    expect(selection).toEqual({ kind: 'constraint', id: 'c:query_rule:Jobs.fee_unit' });
  });

  it('query_rule 选中：编辑面渲染 expression，onChange → constraint.update', () => {
    const { execute } = renderPanel({ kind: 'constraint', id: 'c:query_rule:Jobs.status' });

    const textarea = screen.getByPlaceholderText('rule.expressionPlaceholder') as HTMLTextAreaElement;
    expect(textarea.value).toBe('口径原文');
    fireEvent.change(textarea, { target: { value: '新口径' } });
    expect(execute).toHaveBeenCalledTimes(1);
    const [command] = execute.mock.calls[0]!;
    expect(command).toMatchObject({
      type: 'constraint.update',
      constraintId: 'c:query_rule:Jobs.status',
      constraint: { type: 'query_rule', expression: '新口径' },
    });
  });

  it('severity 行已隐藏（服务端 7 键无此字段，留着 = 假持久化）', () => {
    renderPanel({ kind: 'constraint', id: 'c:query_rule:Jobs.status' });
    expect(screen.queryByText('rule.severity')).toBeNull();
  });

  it('抽象态（fieldSet="abstract"）：不渲染规则段', () => {
    renderPanel({ kind: 'entity', id: 'e:Jobs' }, 'abstract');
    expect(screen.queryByText('rule.listTitle')).toBeNull();
  });

  it('关系段：列出规则标题 + 「加规则」（无下拉）→ constraint.add 并选中', () => {
    const { execute } = renderPanel({ kind: 'relation', id: 'r:OWNS' });

    expect(screen.getByText('rule.listTitle')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'rule.add' }));

    expect(execute).toHaveBeenCalledTimes(1);
    const [command, selection] = execute.mock.calls[0]!;
    expect(command.type).toBe('constraint.add');
    expect((command as { constraint: Constraint }).constraint).toMatchObject({
      type: 'relation_cardinality', relationId: 'r:OWNS', min: 0, max: 1, severity: 'error',
    });
    expect(selection).toEqual({ kind: 'constraint', id: 'c:relation_cardinality:OWNS' });
  });

  it('关系段已有基数规则：再点「加规则」→ 不建（execute 0 次），message.warning 一次', () => {
    const warning = vi.spyOn(message, 'warning').mockReturnValue(undefined as never);
    const { execute } = renderPanel(
      { kind: 'relation', id: 'r:OWNS' },
      undefined,
      [{ id: 'c:relation_cardinality:OWNS', type: 'relation_cardinality', relationId: 'r:OWNS', min: 0, max: 1, severity: 'error' }],
    );

    fireEvent.click(screen.getByRole('button', { name: 'rule.add' }));

    expect(execute).not.toHaveBeenCalled();
    expect(warning).toHaveBeenCalledTimes(1);
  });

  it('属性 Select 改到已被同档占用的属性 → 不 execute、message.warning（rule.duplicate）', async () => {
    // 同档同目标会出现两条约束 → 保存时 name 撞车 → 服务端 name 唯一校验 400、整次保存失败。
    // 三个 Select 的 comboboxes 顺序＝类型 / 实体 / 属性（属性在索引 2）。
    const warning = vi.spyOn(message, 'warning').mockReturnValue(undefined as never);
    const { execute } = renderPanel(
      { kind: 'constraint', id: 'c:query_rule:Jobs.status' },
      undefined,
      [{ id: 'c:query_rule:Jobs.fee_unit', type: 'query_rule', entityId: 'e:Jobs', attributeId: 'a:Jobs.fee_unit', severity: 'error', expression: '' }],
    );

    fireEvent.mouseDown(screen.getAllByRole('combobox')[2]!);
    fireEvent.click(await screen.findByTitle('fee_unit'));

    expect(execute).not.toHaveBeenCalled();
    expect(warning).toHaveBeenCalledTimes(1);
    expect(warning).toHaveBeenCalledWith('rule.duplicate');
  });

  it('实体 Select 改选：自动落到未被同档占用的属性（跳过被占的 id）', async () => {
    const { execute } = renderPanel(
      { kind: 'constraint', id: 'c:query_rule:Jobs.status' },
      undefined,
      [{ id: 'c:query_rule:Dept.id', type: 'query_rule', entityId: 'e:Dept', attributeId: 'a:Dept.id', severity: 'error', expression: '' }],
      [{
        id: 'e:Dept', name: 'Dept', aliases: [],
        attributes: [
          { id: 'a:Dept.id', name: 'id', type: 'number', required: true, primaryKey: true },
          { id: 'a:Dept.name', name: 'name', type: 'string', required: false, primaryKey: false },
        ],
      }],
    );

    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]!);
    fireEvent.click(await screen.findByTitle('Dept'));

    expect(execute).toHaveBeenCalledTimes(1);
    const [command] = execute.mock.calls[0]!;
    expect(command).toMatchObject({
      type: 'constraint.update',
      constraintId: 'c:query_rule:Jobs.status',
      constraint: { entityId: 'e:Dept', attributeId: 'a:Dept.name' },
    });
  });

  it('关系 Select 改到已被另一条基数规则占用的关系 → 不 execute、message.warning（rule.duplicate）', async () => {
    // 同一关系出现两条 relation_cardinality → 保存时 name 撞车、服务端 400 整次保存失败（与属性闸同理）。
    // 约束面板（relation_cardinality）里 combobox 顺序＝类型 / 关系（min/max 是 Input，不是 combobox）。
    const warning = vi.spyOn(message, 'warning').mockReturnValue(undefined as never);
    const { execute } = renderPanel(
      { kind: 'constraint', id: 'c:relation_cardinality:OWNS' },
      undefined,
      [
        { id: 'c:relation_cardinality:OWNS', type: 'relation_cardinality', relationId: 'r:OWNS', min: 0, max: 1, severity: 'error' },
        { id: 'c:relation_cardinality:WORKS_AT', type: 'relation_cardinality', relationId: 'r:WORKS_AT', min: 0, max: 1, severity: 'error' },
      ],
      [],
      [{ id: 'r:WORKS_AT', name: 'WORKS_AT', sourceEntityId: 'e:Jobs', targetEntityId: 'e:Jobs' }],
    );

    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]!);
    fireEvent.click(await screen.findByTitle('WORKS_AT'));

    expect(execute).not.toHaveBeenCalled();
    expect(warning).toHaveBeenCalledTimes(1);
    expect(warning).toHaveBeenCalledWith('rule.duplicate');
  });

  it('关系 Select 改到未被占用的关系 → constraint.update 落该关系（反面对照）', async () => {
    const { execute } = renderPanel(
      { kind: 'constraint', id: 'c:relation_cardinality:OWNS' },
      undefined,
      [
        { id: 'c:relation_cardinality:OWNS', type: 'relation_cardinality', relationId: 'r:OWNS', min: 0, max: 1, severity: 'error' },
        { id: 'c:relation_cardinality:WORKS_AT', type: 'relation_cardinality', relationId: 'r:WORKS_AT', min: 0, max: 1, severity: 'error' },
      ],
      [],
      [
        { id: 'r:WORKS_AT', name: 'WORKS_AT', sourceEntityId: 'e:Jobs', targetEntityId: 'e:Jobs' },
        { id: 'r:MANAGES', name: 'MANAGES', sourceEntityId: 'e:Jobs', targetEntityId: 'e:Jobs' },
      ],
    );

    fireEvent.mouseDown(screen.getAllByRole('combobox')[1]!);
    fireEvent.click(await screen.findByTitle('MANAGES'));

    expect(execute).toHaveBeenCalledTimes(1);
    const [command] = execute.mock.calls[0]!;
    expect(command).toMatchObject({
      type: 'constraint.update',
      constraintId: 'c:relation_cardinality:OWNS',
      constraint: { type: 'relation_cardinality', relationId: 'r:MANAGES' },
    });
  });

  it('抽象态（fieldSet="abstract"）也渲染关系三键；勾选额外类型 → relation.update 带 sourceTypes', () => {
    const extraEntities: EntityType[] = [
      { id: 'e:User', name: 'User', aliases: [], attributes: [] },
    ];
    const { execute } = renderPanel({ kind: 'relation', id: 'r:OWNS' }, 'abstract', [], extraEntities);

    expect(screen.getByText('relation.sourceTypes')).toBeTruthy();
    expect(screen.getByText('relation.targetTypes')).toBeTruthy();
    expect(screen.getByText('relation.inverseOf')).toBeTruthy();

    // 源类型：加一个额外类型 User → onChange([single, extra]) → 父级写 sourceTypes
    // 按钮可访问名含图标的 aria-label（"plus relation.typeAdd"，antd 图标并入命名），用正则。
    const addButtons = screen.getAllByRole('button', { name: /relation.typeAdd/ });
    fireEvent.click(addButtons[0]!);
    fireEvent.click(screen.getByRole('option', { name: /User/ }));

    const [command] = execute.mock.calls[execute.mock.calls.length - 1]!;
    expect(command).toMatchObject({
      type: 'relation.update', relationId: 'r:OWNS',
      changes: { sourceTypes: ['e:Jobs', 'e:User'] },
    });
  });

  it('抽象态渲染降 1 提示；编译态不渲染（守住编译画布不变）', () => {
    renderPanel({ kind: 'relation', id: 'r:OWNS' }, 'abstract');
    expect(screen.getByText('relation.multiEndpointHint')).toBeTruthy();

    cleanup();
    renderPanel({ kind: 'relation', id: 'r:OWNS' });
    expect(screen.queryByText('relation.multiEndpointHint')).toBeNull();
  });

  it('删除实体确认：多态端点引用该实体的关系也计入 typeRefs', async () => {
    const extraEntities: EntityType[] = [
      { id: 'e:Other', name: 'Other', aliases: [], attributes: [] },
    ];
    const extraRelations: RelationType[] = [
      { id: 'r:REF', name: 'REF', sourceEntityId: 'e:Jobs', targetEntityId: 'e:Jobs',
        sourceTypes: ['e:Jobs', 'e:Other'] },
      { id: 'r:BOTH', name: 'BOTH', sourceEntityId: 'e:Other', targetEntityId: 'e:Jobs',
        sourceTypes: ['e:Other', 'e:Jobs'] },
    ];
    renderPanel({ kind: 'entity', id: 'e:Other' }, undefined, [], extraEntities, extraRelations);

    fireEvent.click(screen.getByRole('button', { name: 'entity.deleteTitle' }));

    // t 被 mock 成 key + JSON.stringify(params)。r:BOTH 端点命中 e:Other → 计入 relations；
    // 它的多态集合也含 e:Other，但因端点已命中，必须**被排除**在 typeRefs 之外（否则双重计数）。
    // r:REF 仅集合引用 → 计入 typeRefs。Popconfirm 内容走 portal 且挂载是异步的，用 findByText 等它出现。
    expect(await screen.findByText(/"relations":1/)).toBeTruthy();
    expect(await screen.findByText(/"typeRefs":1/)).toBeTruthy();
  });
});

describe('PropertiesPanel 抽象态：面板级 schemaCache 真传递', () => {
  const landingEntity: EntityType = {
    id: 'e:地址', name: '地址', aliases: [],
    landings: [{ dataSourceId: 'ds_a', entityName: 'Company', included: true, sourceName: '锦聘', stale: false }],
    attributes: [
      { id: 'a:地址.城市', name: '城市', type: 'string', required: false, primaryKey: false },
      { id: 'a:地址.街道', name: '街道', type: 'string', required: false, primaryKey: false },
    ],
  };

  it('两个属性共用 PropertiesPanel 内部的 schemaCache → 同一 ds 只拉一次', async () => {
    getAllCompiledSchema.mockResolvedValue({
      entity_types: [{ name: 'Company', attributes: [{ name: '城市' }] }],
    });
    renderPanel({ kind: 'entity', id: 'e:地址' }, 'abstract', [], [landingEntity], [], 'kb_1');

    const suggests = screen.getAllByText('attributeSources.suggest');
    expect(suggests).toHaveLength(2);

    fireEvent.click(suggests[0]!);
    await screen.findAllByText('attributeSources.suggestTitle');
    fireEvent.click(suggests[1]!);
    await waitFor(() => expect(getAllCompiledSchema).toHaveBeenCalledTimes(1));
  });
});
