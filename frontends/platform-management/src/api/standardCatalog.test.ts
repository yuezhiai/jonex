/**
 * `standardCatalog` 取数模块 —— 纯函数映射 + 接口封装。
 *
 * 断言钉住五件事：
 *   ① 各 mapper 的 snake_case → camelCase，缺字段按「零/空/None」兜底不崩；
 *   ② `is_primary_key` / `is_required` 的 0/1 归一为 boolean；
 *   ③ `doc_stats` 缺省或部分缺省补 0，额外键不外泄；
 *   ④ `listKbSchemas` 查询参数 snake_case，空 keyword 不传；
 *   ⑤ `getStandardReferences` 的 `source_id_coverage` / `truncated` 映射。
 *
 * 不连真实网络：`./request` 整体 mock。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from './request';
import {
  diffScenarioVersions,
  getScenarioVersion,
  getStandardDomain,
  getStandardReferences,
  getStandardScenario,
  listKbSchemas,
  listScenarioVersions,
  listStandardDomains,
  mapAttribute,
  mapConstraint,
  mapDocStats,
  mapDomain,
  mapKbSchema,
  getStandardCompliance,
  mapCompliance,
  mapObject,
  mapReference,
  mapRelation,
  mapScenario,
  mapVersionDiff,
} from './standardCatalog';

vi.mock('./request', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

type DomainRow = Parameters<typeof mapDomain>[0];
type ScenarioRow = Parameters<typeof mapScenario>[0];
type ObjectRow = Parameters<typeof mapObject>[0];
type AttributeRow = Parameters<typeof mapAttribute>[0];
type RelationRow = Parameters<typeof mapRelation>[0];
type ConstraintRow = Parameters<typeof mapConstraint>[0];
type KbSchemaRow = Parameters<typeof mapKbSchema>[0];

function makeDocStatsRow() {
  return {
    doc_total: 120,
    no_target: 5,
    not_applied: 3,
    applied_current: 112,
    ontology_ready: 110,
  };
}

function makeDomainRow(overrides: Partial<DomainRow> = {}): DomainRow {
  return {
    id: 'tpl_dom_finance',
    name: '金融',
    name_en: 'Finance',
    description: '金融领域',
    status: 'active',
    version: 3,
    published_at: '2026-10-01T00:00:00Z',
    structure_hash: 'hash-domain',
    scenario_count: 3,
    object_count: 42,
    latest_published_at: '2026-10-02T00:00:00Z',
    ...overrides,
  };
}

function makeScenarioRow(overrides: Partial<ScenarioRow> = {}): ScenarioRow {
  return {
    id: 'sc-1',
    domain_id: 'tpl_dom_finance',
    name: '信贷风控',
    description: '信贷风控场景',
    version: 2,
    published_at: '2026-10-03T00:00:00Z',
    structure_hash: 'hash-scenario',
    object_count: 12,
    attribute_count: 80,
    relation_count: 9,
    constraint_count: 4,
    ...overrides,
  };
}

function makeAttributeRow(overrides: Partial<AttributeRow> = {}): AttributeRow {
  return {
    id: 'attr-1',
    attr_name: '客户号',
    description: '客户唯一标识',
    attr_type: 'string',
    is_primary_key: 1,
    is_required: 1,
    ontology_code: 'customer_id',
    constraints_json: { max_length: 32 },
    ...overrides,
  };
}

function makeObjectRow(overrides: Partial<ObjectRow> = {}): ObjectRow {
  return {
    id: 'obj-1',
    name: '客户',
    description: '客户对象',
    status: 'active',
    ontology_code: 'Customer',
    aliases: ['用户', '客户方'],
    attributes: [
      makeAttributeRow(),
      makeAttributeRow({
        id: 'attr-2',
        attr_name: '等级',
        description: null,
        attr_type: null,
        is_primary_key: 0,
        is_required: 0,
        ontology_code: null,
        constraints_json: null,
      }),
    ],
    ...overrides,
  };
}

function makeRelationRow(overrides: Partial<RelationRow> = {}): RelationRow {
  return {
    id: 'rel-1',
    name: '持有',
    description: '客户持有账户',
    relation_type: 'one_to_many',
    status: 'active',
    source_object_id: 'obj-1',
    target_object_id: 'obj-2',
    ontology_code: 'holds',
    ...overrides,
  };
}

function makeConstraintRow(overrides: Partial<ConstraintRow> = {}): ConstraintRow {
  return {
    id: 'con-1',
    name: '金额必须为正',
    target_type: 'attribute',
    target_id: 'attr-3',
    target_label: '金额',
    constraint_type: 'range',
    expression: '> 0',
    suggestion: '请填写正数',
    ...overrides,
  };
}

function makeKbSchemaRow(overrides: Partial<KbSchemaRow> = {}): KbSchemaRow {
  return {
    kb_id: 'kb-1',
    kb_name: '财报库',
    space_id: 'space-1',
    scenario_id: 'sc-1',
    scenario_name: '财报分析',
    binding_scenario_id: 'sc-1',
    compiled_scenario_id: 'sc-1',
    domain_id: 'tpl_dom_finance',
    domain_name: '金融',
    binding_status: 'active',
    source_type: 'business_template',
    source_version: 2,
    source_hash: 'hash-source',
    schema_version: 5,
    schema_mode: 'template_seeded',
    sync_status: 'synced',
    compiled_at: '2026-10-04T00:00:00Z',
    doc_stats: makeDocStatsRow(),
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe('mapDocStats', () => {
  it('整体缺省（undefined/null）时五键全为 0', () => {
    const zero = {
      docTotal: 0,
      noTarget: 0,
      notApplied: 0,
      appliedCurrent: 0,
      ontologyReady: 0,
    };
    expect(mapDocStats()).toEqual(zero);
    expect(mapDocStats(null)).toEqual(zero);
  });

  it('部分缺省按 0 补，额外键不外泄', () => {
    expect(
      mapDocStats({ doc_total: 7, applied_current: 3, unknown_key: 99 }),
    ).toEqual({
      docTotal: 7,
      noTarget: 0,
      notApplied: 0,
      appliedCurrent: 3,
      ontologyReady: 0,
    });
  });

  it('五键齐全时逐键映射', () => {
    expect(mapDocStats(makeDocStatsRow())).toEqual({
      docTotal: 120,
      noTarget: 5,
      notApplied: 3,
      appliedCurrent: 112,
      ontologyReady: 110,
    });
  });
});

describe('mapDomain / mapScenario', () => {
  it('mapDomain 全字段 snake→camel', () => {
    expect(mapDomain(makeDomainRow())).toEqual({
      id: 'tpl_dom_finance',
      name: '金融',
      nameEn: 'Finance',
      description: '金融领域',
      status: 'active',
      version: 3,
      publishedAt: '2026-10-01T00:00:00Z',
      structureHash: 'hash-domain',
      scenarioCount: 3,
      objectCount: 42,
      latestPublishedAt: '2026-10-02T00:00:00Z',
    });
  });

  it('mapDomain 详情行缺计数时 mapped 不含这些键（不把「未统计」伪装成 0）', () => {
    const row: DomainRow = { id: 'dom-2', name: '零售' };
    const mapped = mapDomain(row);

    expect(mapped).toEqual({
      id: 'dom-2',
      name: '零售',
      nameEn: null,
      description: null,
      status: null,
      version: null,
      publishedAt: null,
      structureHash: null,
    });
    expect(mapped).not.toHaveProperty('scenarioCount');
    expect(mapped).not.toHaveProperty('objectCount');
    expect(mapped).not.toHaveProperty('latestPublishedAt');
  });

  it('mapScenario 全字段 snake→camel（含四类计数）', () => {
    expect(mapScenario(makeScenarioRow())).toEqual({
      id: 'sc-1',
      domainId: 'tpl_dom_finance',
      name: '信贷风控',
      description: '信贷风控场景',
      version: 2,
      publishedAt: '2026-10-03T00:00:00Z',
      structureHash: 'hash-scenario',
      objectCount: 12,
      attributeCount: 80,
      relationCount: 9,
      constraintCount: 4,
    });
  });

  it('mapScenario 缺计数时不写计数键，缺发布信息时补 null', () => {
    const mapped = mapScenario({ id: 'sc-2', name: '空场景' });

    expect(mapped.domainId).toBeUndefined();
    expect(mapped.description).toBeNull();
    expect(mapped.version).toBeNull();
    expect(mapped.publishedAt).toBeNull();
    expect(mapped.structureHash).toBeNull();
    expect(mapped).not.toHaveProperty('objectCount');
    expect(mapped).not.toHaveProperty('attributeCount');
    expect(mapped).not.toHaveProperty('relationCount');
    expect(mapped).not.toHaveProperty('constraintCount');
  });
});

describe('mapObject / mapAttribute', () => {
  it('mapAttribute 把 0/1 归一为 boolean，constraints_json 缺失补 {}', () => {
    expect(mapAttribute(makeAttributeRow())).toEqual({
      id: 'attr-1',
      attrName: '客户号',
      description: '客户唯一标识',
      attrType: 'string',
      isPrimaryKey: true,
      isRequired: true,
      ontologyCode: 'customer_id',
      constraintsJson: { max_length: 32 },
    });

    expect(
      mapAttribute({ id: 'attr-9', attr_name: '备注', is_primary_key: 0, is_required: 0 }),
    ).toEqual({
      id: 'attr-9',
      attrName: '备注',
      description: '',
      attrType: null,
      isPrimaryKey: false,
      isRequired: false,
      ontologyCode: null,
      constraintsJson: {},
    });
  });

  it('mapObject 映射 aliases 与嵌套 attributes，缺失时补 []', () => {
    expect(mapObject(makeObjectRow())).toEqual({
      id: 'obj-1',
      name: '客户',
      description: '客户对象',
      status: 'active',
      ontologyCode: 'Customer',
      aliases: ['用户', '客户方'],
      attributes: [
        {
          id: 'attr-1',
          attrName: '客户号',
          description: '客户唯一标识',
          attrType: 'string',
          isPrimaryKey: true,
          isRequired: true,
          ontologyCode: 'customer_id',
          constraintsJson: { max_length: 32 },
        },
        {
          id: 'attr-2',
          attrName: '等级',
          description: '',
          attrType: null,
          isPrimaryKey: false,
          isRequired: false,
          ontologyCode: null,
          constraintsJson: {},
        },
      ],
    });

    expect(mapObject({ id: 'obj-2', name: '空对象' })).toEqual({
      id: 'obj-2',
      name: '空对象',
      description: '',
      status: null,
      ontologyCode: null,
      aliases: [],
      attributes: [],
    });
  });
});

describe('mapRelation / mapConstraint', () => {
  it('mapRelation 全字段 snake→camel', () => {
    expect(mapRelation(makeRelationRow())).toEqual({
      id: 'rel-1',
      name: '持有',
      description: '客户持有账户',
      relationType: 'one_to_many',
      status: 'active',
      sourceObjectId: 'obj-1',
      targetObjectId: 'obj-2',
      ontologyCode: 'holds',
    });
  });

  it('mapRelation 缺可选字段时 description 补空串、其余补 null', () => {
    expect(mapRelation({ id: 'rel-2', name: '孤立关系' })).toEqual({
      id: 'rel-2',
      name: '孤立关系',
      description: '',
      relationType: null,
      status: null,
      sourceObjectId: null,
      targetObjectId: null,
      ontologyCode: null,
    });
  });

  it('mapConstraint 全字段 snake→camel，expression/suggestion 可空', () => {
    expect(mapConstraint(makeConstraintRow())).toEqual({
      id: 'con-1',
      name: '金额必须为正',
      targetType: 'attribute',
      targetId: 'attr-3',
      targetLabel: '金额',
      constraintType: 'range',
      expression: '> 0',
      suggestion: '请填写正数',
    });

    expect(
      mapConstraint({
        id: 'con-2',
        name: '必填',
        target_type: 'object',
        target_id: 'obj-1',
        target_label: '客户',
        constraint_type: 'required',
      }),
    ).toMatchObject({ expression: null, suggestion: null });
  });
});

describe('mapKbSchema / mapReference', () => {
  it('mapKbSchema 全字段映射（含 doc_stats）', () => {
    expect(mapKbSchema(makeKbSchemaRow())).toEqual({
      kbId: 'kb-1',
      kbName: '财报库',
      spaceId: 'space-1',
      scenarioId: 'sc-1',
      scenarioName: '财报分析',
      bindingScenarioId: 'sc-1',
      compiledScenarioId: 'sc-1',
      domainId: 'tpl_dom_finance',
      domainName: '金融',
      bindingStatus: 'active',
      sourceType: 'business_template',
      sourceVersion: 2,
      sourceHash: 'hash-source',
      schemaVersion: 5,
      schemaMode: 'template_seeded',
      syncStatus: 'synced',
      compiledAt: '2026-10-04T00:00:00Z',
      docStats: {
        docTotal: 120,
        noTarget: 5,
        notApplied: 3,
        appliedCurrent: 112,
        ontologyReady: 110,
      },
    });
  });

  it('mapKbSchema 的 binding_status 缺省/空串都归一 none，doc_stats 缺省全 0', () => {
    const mapped = mapKbSchema({
      kb_id: 'kb-2',
      kb_name: '孤儿库',
      binding_status: null,
    });

    expect(mapped.bindingStatus).toBe('none');
    expect(
      mapKbSchema({ kb_id: 'kb-2', kb_name: '孤儿库', binding_status: '' })
        .bindingStatus,
    ).toBe('none');
    expect(mapped.spaceId).toBeNull();
    expect(mapped.scenarioId).toBeNull();
    expect(mapped.scenarioName).toBeNull();
    expect(mapped.bindingScenarioId).toBeNull();
    expect(mapped.compiledScenarioId).toBeNull();
    expect(mapped.domainId).toBeNull();
    expect(mapped.domainName).toBeNull();
    expect(mapped.sourceType).toBeNull();
    expect(mapped.sourceVersion).toBeNull();
    expect(mapped.sourceHash).toBeNull();
    expect(mapped.schemaVersion).toBeNull();
    expect(mapped.schemaMode).toBeNull();
    expect(mapped.syncStatus).toBeNull();
    expect(mapped.compiledAt).toBeNull();
    expect(mapped.docStats).toEqual({
      docTotal: 0,
      noTarget: 0,
      notApplied: 0,
      appliedCurrent: 0,
      ontologyReady: 0,
    });
  });

  it('mapReference 只取引用字段，行里没有 scenario/domain 字段也不崩', () => {
    const row: KbSchemaRow = {
      kb_id: 'kb-1',
      kb_name: '财报库',
      space_id: null,
      binding_status: 'none',
      source_type: 'compiled_only',
      binding_scenario_id: null,
      compiled_scenario_id: 'sc-9',
      source_version: null,
      schema_version: 7,
      sync_status: 'outdated',
      schema_mode: 'template_seeded',
      doc_stats: { doc_total: 5 },
    };

    expect(mapReference(row)).toEqual({
      kbId: 'kb-1',
      kbName: '财报库',
      spaceId: null,
      bindingStatus: 'none',
      sourceType: 'compiled_only',
      bindingScenarioId: null,
      compiledScenarioId: 'sc-9',
      sourceVersion: null,
      schemaVersion: 7,
      syncStatus: 'outdated',
      schemaMode: 'template_seeded',
      docStats: {
        docTotal: 5,
        noTarget: 0,
        notApplied: 0,
        appliedCurrent: 0,
        ontologyReady: 0,
      },
    });
  });

  it('mapReference 的 binding_status 缺省/空串都归一 none', () => {
    expect(
      mapReference({ kb_id: 'kb-3', kb_name: '无绑定库' }).bindingStatus,
    ).toBe('none');
    expect(
      mapReference({ kb_id: 'kb-3', kb_name: '无绑定库', binding_status: '' })
        .bindingStatus,
    ).toBe('none');
  });
});

describe('接口封装', () => {
  it('listStandardDomains 走标准目录端点并映射 items，缺 items 时返回空数组', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [makeDomainRow()],
      total: 1,
    });

    const domains = await listStandardDomains();

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/standards/domains');
    expect(domains).toHaveLength(1);
    expect(domains[0].id).toBe('tpl_dom_finance');
    expect(domains[0].scenarioCount).toBe(3);

    vi.mocked(apiClient.get).mockResolvedValue({});
    await expect(listStandardDomains()).resolves.toEqual([]);
  });

  it('getStandardDomain 路径编码并映射 domain/scenarios，缺 scenarios 返回空数组', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      domain: makeDomainRow({ id: 'tpl/d o' }),
      scenarios: [makeScenarioRow()],
    });

    const result = await getStandardDomain('tpl/d o');

    expect(apiClient.get).toHaveBeenCalledWith(
      '/data-governance/standards/domains/tpl%2Fd%20o',
    );
    expect(result.domain.id).toBe('tpl/d o');
    expect(result.scenarios).toHaveLength(1);
    expect(result.scenarios[0].attributeCount).toBe(80);

    vi.mocked(apiClient.get).mockResolvedValue({ domain: makeDomainRow() });
    await expect(getStandardDomain('d')).resolves.toMatchObject({ scenarios: [] });
  });

  it('getStandardScenario 路径编码并映射 scenario/objects/relations/constraints', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      scenario: makeScenarioRow({ id: 'sc/1' }),
      objects: [makeObjectRow()],
      relations: [makeRelationRow()],
      constraints: [makeConstraintRow()],
    });

    const result = await getStandardScenario('sc/1');

    expect(apiClient.get).toHaveBeenCalledWith(
      '/data-governance/standards/scenarios/sc%2F1',
    );
    expect(result.scenario.id).toBe('sc/1');
    expect(result.objects[0].attributes[0].isPrimaryKey).toBe(true);
    expect(result.relations[0].relationType).toBe('one_to_many');
    expect(result.constraints[0].constraintType).toBe('range');
  });

  it('getStandardScenario 数组缺省时全部返回空数组', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ scenario: makeScenarioRow() });

    await expect(getStandardScenario('sc-1')).resolves.toMatchObject({
      objects: [],
      relations: [],
      constraints: [],
    });
  });

  it('listKbSchemas 参数 snake_case，keyword 去空白后非空才传', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [makeKbSchemaRow()],
      total: 1,
    });

    const result = await listKbSchemas({
      kbId: 'kb-1',
      keyword: ' 财报 ',
      page: 2,
      pageSize: 20,
    });

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/standards/kb-schemas', {
      params: {
        kb_id: 'kb-1',
        keyword: '财报',
        page: 2,
        page_size: 20,
      },
    });
    expect(result.total).toBe(1);
    expect(result.items[0].kbId).toBe('kb-1');
  });

  it('listKbSchemas 空 keyword / 空白 kbId 时不传对应参数，total 缺省 0', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({});

    const result = await listKbSchemas({
      kbId: '   ',
      keyword: '   ',
      page: 1,
      pageSize: 10,
    });

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/standards/kb-schemas', {
      params: { page: 1, page_size: 10 },
    });
    expect(result).toEqual({ items: [], total: 0 });
  });

  it('getStandardReferences 映射 source_id_coverage/truncated 与 items', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      scope_type: 'scenario',
      scope_id: 'sc-1',
      items: [makeKbSchemaRow()],
      total: 1,
      truncated: true,
      source_id_coverage: 'partial',
    });

    const result = await getStandardReferences('scenario', 'sc-1');

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/standards/references', {
      params: { scope_type: 'scenario', scope_id: 'sc-1' },
    });
    expect(result.scopeType).toBe('scenario');
    expect(result.scopeId).toBe('sc-1');
    expect(result.total).toBe(1);
    expect(result.truncated).toBe(true);
    expect(result.sourceIdCoverage).toBe('partial');
    expect(result.items[0].docStats.docTotal).toBe(120);
  });

  it('getStandardReferences 缺省时 truncated=false、coverage 回落 partial、items 空', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      scope_type: 'object',
      scope_id: 'obj-1',
    });

    const result = await getStandardReferences('object', 'obj-1');

    expect(result.items).toEqual([]);
    expect(result.total).toBe(0);
    expect(result.truncated).toBe(false);
    expect(result.sourceIdCoverage).toBe('partial');
  });
});

// ── 发布版本历史与 diff（第 6 项第二增量） ─────────────────────────────

describe('standardCatalog · 发布版本历史', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('listScenarioVersions 路径编码并映射 snake_case', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [
        {
          id: 'ver-2',
          version: 2,
          structure_hash: 'abc123',
          published_by: 'admin',
          published_at: '2026-10-18T10:00:00+00:00',
        },
      ],
      total: 1,
    });

    const result = await listScenarioVersions('sc/1');

    expect(apiClient.get).toHaveBeenCalledWith(
      '/data-governance/standards/scenarios/sc%2F1/versions',
    );
    expect(result.total).toBe(1);
    expect(result.items[0]).toEqual({
      id: 'ver-2',
      version: 2,
      structureHash: 'abc123',
      publishedBy: 'admin',
      publishedAt: '2026-10-18T10:00:00+00:00',
    });
  });

  it('getScenarioVersion 透传 snapshot 并补齐空值', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      id: 'ver-1',
      version: 1,
      snapshot: { objects: [] },
    });

    const result = await getScenarioVersion('sc-1', 1);

    expect(apiClient.get).toHaveBeenCalledWith(
      '/data-governance/standards/scenarios/sc-1/versions/1',
    );
    expect(result.structureHash).toBeNull();
    expect(result.publishedBy).toBeNull();
    expect(result.snapshot).toEqual({ objects: [] });
  });

  it('diffScenarioVersions 传 from_version/to_version 并映射实体差异', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      from_version: 1,
      to_version: 2,
      scenario: {
        changed: [{ field: 'name', from: '旧', to: '新' }],
      },
      objects: {
        added: [{ id: 'o2', name: '客户', ontology_code: 'Customer' }],
        removed: [],
        changed: [
          { id: 'o1', label: '订单', fields: [{ field: 'status', from: 'draft', to: 'active' }] },
        ],
      },
      attributes: { added: [], removed: [], changed: [] },
      relations: { added: [], removed: [], changed: [] },
      constraints: { added: [], removed: [], changed: [] },
      summary: {
        scenario_changed: 1,
        objects_added: 1,
        objects_removed: 0,
        objects_changed: 1,
        attributes_added: 0,
        attributes_removed: 0,
        attributes_changed: 0,
        relations_added: 0,
        relations_removed: 0,
        relations_changed: 0,
        constraints_added: 0,
        constraints_removed: 0,
        constraints_changed: 0,
      },
    });

    const result = await diffScenarioVersions('sc-1', 1, 2);

    expect(apiClient.get).toHaveBeenCalledWith(
      '/data-governance/standards/scenarios/sc-1/versions-diff',
      { params: { from_version: 1, to_version: 2 } },
    );
    expect(result.scenario.changed[0]).toEqual({
      field: 'name',
      from: '旧',
      to: '新',
    });
    expect(result.objects.added[0]).toMatchObject({
      id: 'o2',
      label: '客户',
      details: { ontology_code: 'Customer' },
    });
    expect(result.objects.changed[0].fields?.[0]).toEqual({
      field: 'status',
      from: 'draft',
      to: 'active',
    });
    expect(result.summary.objects_added).toBe(1);
  });

  it('mapVersionDiff 缺省分组返回空数组而不是崩溃', () => {
    const result = mapVersionDiff({ from_version: 1, to_version: 2 });
    expect(result.objects).toEqual({ added: [], removed: [], changed: [] });
    expect(result.attributes).toEqual({ added: [], removed: [], changed: [] });
    expect(result.scenario.changed).toEqual([]);
    expect(result.summary).toEqual({});
  });
});

// ── 落标校验（第二增量） ──────────────────────────────────────────────

describe('standardCatalog · 落标校验', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('getStandardCompliance 路径编码并映射状态/差异/文档统计', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      scenario_id: 'sc/1',
      latest_published_version: 3,
      latest_structure_hash: 'h3',
      items: [
        {
          kb_id: 'kb-1',
          kb_name: 'KB1',
          status: 'outdated',
          latest_version: 3,
          source_version: 2,
          schema_version: 5,
          sync_status: 'outdated',
          missing_objects: [{ id: 'o1', label: '订单' }],
          extra_attributes: [
            { id: 'a1', label: '额外字段', object: '订单' },
          ],
          changed_attributes: [
            {
              id: 'a2', label: '金额', object: '订单',
              fields: [{ field: 'type', standard: 'number', compiled: 'string' }],
            },
          ],
          doc_stats: { doc_total: 4 },
        },
      ],
      summary: { total: 1, outdated: 1 },
    });

    const result = await getStandardCompliance('sc/1');

    expect(apiClient.get).toHaveBeenCalledWith(
      '/data-governance/standards/scenarios/sc%2F1/compliance',
    );
    expect(result.latestPublishedVersion).toBe(3);
    const item = result.items[0];
    expect(item.kbId).toBe('kb-1');
    expect(item.status).toBe('outdated');
    expect(item.missingObjects[0]).toEqual({ id: 'o1', label: '订单' });
    expect(item.extraAttributes[0]).toMatchObject({
      id: 'a1', label: '额外字段', object: '订单',
    });
    expect(item.changedAttributes[0].fields?.[0]).toEqual({
      field: 'type', standard: 'number', compiled: 'string',
    });
    expect(item.docStats.docTotal).toBe(4);
    expect(item.docStats.noTarget).toBe(0);
    expect(result.summary).toEqual({ total: 1, outdated: 1 });
  });

  it('mapCompliance 缺省 items/summary 返回空而不崩', () => {
    const result = mapCompliance({ scenario_id: 'sc-1' });
    expect(result.items).toEqual([]);
    expect(result.summary).toEqual({});
    expect(result.latestPublishedVersion).toBeNull();
  });
});
