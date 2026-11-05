/**
 * `resourceViews` 取数模块 —— 纯映射/参数组装 + 9 个接口封装。
 *
 * 断言钉住：
 *   ① 参数 camelCase → snake_case：空串丢弃、分页归一；
 *   ② 响应行 snake → camel、null / 缺失字段容错；
 *   ③ 覆盖率 null 保持 null（不伪造成 0）；
 *   ④ 责任视图兼容后端 `groups` 与页面侧 `rows` 两种键；
 *   ⑤ 9 个 async 封装的 method / path / params / body。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from './request';
import {
  buildBindingListParams,
  buildCoverageParams,
  buildDomainViewParams,
  buildResponsibilityParams,
  createStandardBinding,
  deleteStandardBinding,
  getDomainView,
  getResponsibilityView,
  getSourceAnchors,
  getStandardBindingCoverage,
  listStandardBindings,
  mapAnchorResult,
  mapBinding,
  mapCoverageRow,
  mapDomainGroup,
  mapResponsibilityRow,
  mapSuggestion,
  suggestStandardBindings,
  updateStandardBinding,
} from './resourceViews';

vi.mock('./request', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

const BASE = '/data-governance/resource-views';

const ZERO_SENSITIVITY = {
  public: 0,
  internal: 0,
  confidential: 0,
  secret: 0,
  unclassified: 0,
};

const ZERO_ISSUES = { critical: 0, error: 0, warning: 0, info: 0 };

const ZERO_COUNTS = {
  table: 0,
  column: 0,
  metric: 0,
  document: 0,
  dataSource: 0,
};

function makeBindingRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'b1',
    tenant_id: 't1',
    standard_kind: 'data_element',
    standard_code: 'customer.name',
    standard_asset_id: 'sa1',
    asset_type: 'column',
    asset_id: 'ds:dbo.t.c',
    source: 'manual',
    confidence: 1,
    note: null,
    status: 'active',
    revision: 2,
    created_by: 'u1',
    updated_by: 'u1',
    created_at: '2026-10-19T00:00:00Z',
    updated_at: '2026-10-19T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('参数组装', () => {
  it('业务域参数只发非空筛选', () => {
    expect(buildDomainViewParams({ spaceId: 'sp1', domain: 'Sales' })).toEqual({
      space_id: 'sp1',
      domain: 'Sales',
    });
    expect(buildDomainViewParams({ spaceId: '', domain: '' })).toEqual({});
    expect(buildDomainViewParams({})).toEqual({});
  });

  it('责任参数默认 group_by=owner 并保留空间筛选', () => {
    expect(buildResponsibilityParams({ groupBy: 'domain' })).toEqual({
      group_by: 'domain',
    });
    expect(
      buildResponsibilityParams({ groupBy: 'owner', spaceId: 'sp1' }),
    ).toEqual({ group_by: 'owner', space_id: 'sp1' });
  });

  it('绑定列表参数丢弃空筛选、分页转 snake_case', () => {
    expect(
      buildBindingListParams({
        standardKind: 'data_element',
        standardCode: '',
        assetType: 'column',
        assetId: '',
        status: 'active',
        page: 2,
        pageSize: 50,
      }),
    ).toEqual({
      standard_kind: 'data_element',
      asset_type: 'column',
      status: 'active',
      page: 2,
      page_size: 50,
    });
  });

  it('覆盖率参数只发非空 kind/domain', () => {
    expect(
      buildCoverageParams({ kind: 'metric_card', page: 1, pageSize: 20 }),
    ).toEqual({ kind: 'metric_card', page: 1, page_size: 20 });
    expect(buildCoverageParams({ kind: '', page: 3, pageSize: 10 })).toEqual({
      page: 3,
      page_size: 10,
    });
  });
});

describe('映射（null / 缺失容错）', () => {
  it('mapDomainGroup 空行回零值，完整行 snake→camel', () => {
    expect(mapDomainGroup({})).toEqual({
      spaceId: null,
      spaceName: null,
      domain: '',
      assetCounts: ZERO_COUNTS,
      ownerCount: 0,
      owners: [],
      sensitivity: ZERO_SENSITIVITY,
      openIssues: ZERO_ISSUES,
    });

    const group = mapDomainGroup({
      space_id: 'sp1',
      space_name: '销售空间',
      domain: '__unassigned__',
      asset_counts: { table: 2, column: 3, metric: 1, document: 4, data_source: 5 },
      owner_count: 2,
      owners: [{ owner: 'u1', count: 3 }, { owner: 'u2' }],
      sensitivity: { public: 1, secret: 2 },
      open_issues: { critical: 1, warning: 2 },
    });
    expect(group.spaceId).toBe('sp1');
    expect(group.assetCounts).toEqual({
      table: 2,
      column: 3,
      metric: 1,
      document: 4,
      dataSource: 5,
    });
    expect(group.owners).toEqual([
      { owner: 'u1', count: 3 },
      { owner: 'u2', count: 0 },
    ]);
    expect(group.sensitivity.secret).toBe(2);
    expect(group.sensitivity.unclassified).toBe(0);
    expect(group.openIssues.warning).toBe(2);
  });

  it('mapResponsibilityRow 缺失 owner_count 保持 undefined', () => {
    const row = mapResponsibilityRow({
      key: '__unowned__',
      display_name: '__unowned__',
      asset_counts: { table: 1 },
      sensitivity: { unclassified: 1 },
      open_issues: { info: 1 },
      last_updated_at: null,
    });
    expect(row.ownerCount).toBeUndefined();
    expect(row.lastUpdatedAt).toBeNull();
    expect(row.assetCounts.table).toBe(1);

    expect(mapResponsibilityRow({ owner_count: 0 }).ownerCount).toBe(0);
  });

  it('mapBinding 空行回安全默认值，confidence=0 不被吞', () => {
    expect(mapBinding({})).toEqual({
      id: '',
      tenantId: '',
      standardKind: '',
      standardCode: '',
      standardAssetId: null,
      assetType: 'table',
      assetId: '',
      source: 'manual',
      confidence: null,
      note: null,
      status: 'active',
      revision: 1,
      createdBy: null,
      updatedBy: null,
      createdAt: null,
      updatedAt: null,
    });

    const binding = mapBinding(makeBindingRow({
      source: 'suggested', confidence: 0, status: 'disabled',
    }));
    expect(binding.source).toBe('suggested');
    expect(binding.confidence).toBe(0);
    expect(binding.status).toBe('disabled');
    expect(binding.standardAssetId).toBe('sa1');
    expect(binding.revision).toBe(2);
  });

  it('mapCoverageRow：unbound 缺失时按绑定数推导，bound_assets 映射', () => {
    expect(mapCoverageRow({}).unbound).toBe(true);

    const row = mapCoverageRow({
      standard_asset_id: 'sa1',
      standard_kind: 'data_element',
      standard_code: 'customer.name',
      name: '客户姓名',
      version: 2,
      domain: '客户',
      status: 'published',
      bindings_count: 1,
      bound_assets: [{
        binding_id: 'b1',
        asset_type: 'column',
        asset_id: 'ds:dbo.t.c',
        source: 'suggested',
        confidence: 0.8,
      }, 'bad-row'],
    });
    expect(row.unbound).toBe(false);
    expect(row.bindingsCount).toBe(1);
    expect(row.boundAssets).toHaveLength(2);
    expect(row.boundAssets[0]).toEqual({
      bindingId: 'b1',
      assetType: 'column',
      assetId: 'ds:dbo.t.c',
      source: 'suggested',
      confidence: 0.8,
    });
    expect(row.boundAssets[1].bindingId).toBe('');
  });

  it('mapSuggestion 空行回空串 / null，standard_name 缺失回落 name', () => {
    const suggestion = mapSuggestion({});
    expect(suggestion.standardName).toBe('');
    expect(suggestion.assetType).toBe('column');
    expect(suggestion.confidence).toBeNull();
    expect(suggestion.reason).toBeNull();

    const fallback = mapSuggestion({ name: '订单金额', asset_name: 'amount' });
    expect(fallback.standardName).toBe('订单金额');
    expect(fallback.name).toBe('订单金额');
    expect(fallback.assetName).toBe('amount');
  });

  it('mapAnchorResult 空响应回空链接与三态默认值，完整响应逐层映射', () => {
    expect(mapAnchorResult(null)).toEqual({
      asset: { assetType: 'table', assetId: '' },
      linked: [],
      unresolved: false,
      degraded: false,
    });

    const result = mapAnchorResult({
      asset: { asset_type: 'column', asset_id: 'ds:dbo.t.c' },
      linked: [{
        knowledge_base_id: 'kb1',
        doc_type: '订单',
        entity_name: 'Orders',
        entity_count: 3,
        truncated: true,
        attributes: { 状态: '已支付' },
        documents: [{ id: 'd1', name: '订单说明.pdf', status: 'ready' }],
      }],
      unresolved: false,
      degraded: true,
    });
    expect(result.asset).toEqual({ assetType: 'column', assetId: 'ds:dbo.t.c' });
    expect(result.linked[0].attributes).toEqual({ 状态: '已支付' });
    expect(result.linked[0].documents[0].name).toBe('订单说明.pdf');
    expect(result.linked[0].truncated).toBe(true);
    expect(result.degraded).toBe(true);
  });
});

describe('接口封装', () => {
  it('getDomainView GET domains，映射分组 / 合计', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      groups: [{
        space_id: 'sp1',
        space_name: '销售空间',
        domain: '__unassigned__',
        asset_counts: { table: 2 },
        owner_count: 1,
        owners: [{ owner: 'u1', count: 2 }],
        sensitivity: { public: 2 },
        open_issues: { critical: 1 },
      }],
      totals: { asset_counts: { table: 2 }, groups: 1 },
      empty: false,
    });

    const result = await getDomainView({ spaceId: 'sp1' });

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/domains`, {
      params: { space_id: 'sp1' },
    });
    expect(result.groups[0].assetCounts.table).toBe(2);
    expect(result.groups[0].owners[0]).toEqual({ owner: 'u1', count: 2 });
    expect(result.totals.groups).toBe(1);
    expect(result.empty).toBe(false);
  });

  it('getDomainView 空响应显式 empty=true', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({});
    const result = await getDomainView();
    expect(result.groups).toEqual([]);
    expect(result.totals.groups).toBe(0);
    expect(result.empty).toBe(true);
  });

  it('getResponsibilityView GET responsibilities，兼容后端 groups 键', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      group_by: 'owner',
      groups: [{
        key: 'u1',
        display_name: 'Bob',
        asset_counts: { table: 1 },
        sensitivity: { internal: 1 },
        open_issues: { error: 1 },
        last_updated_at: '2026-01-01T00:00:00Z',
      }, {
        key: '__unowned__',
        display_name: '__unowned__',
      }],
      totals: { unowned: 1, unassigned_domain: 0, groups: 2 },
      empty: false,
    });

    const result = await getResponsibilityView({ groupBy: 'owner' });

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/responsibilities`, {
      params: { group_by: 'owner' },
    });
    expect(result.rows[0].displayName).toBe('Bob');
    expect(result.rows[1].key).toBe('__unowned__');
    expect(result.totals.unowned).toBe(1);
    expect(result.groupBy).toBe('owner');
    expect(result.empty).toBe(false);
  });

  it('getResponsibilityView 也接受页面侧 rows 键', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      rows: [{ key: 'Sales', display_name: 'Sales' }],
      totals: {},
      group_by: 'domain',
    });
    const result = await getResponsibilityView({ groupBy: 'domain' });
    expect(result.rows).toHaveLength(1);
    expect(result.groupBy).toBe('domain');
    expect(result.empty).toBe(false);
  });

  it('listStandardBindings GET standard-bindings', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [makeBindingRow()],
      total: 1,
      page: 2,
      page_size: 20,
    });

    const result = await listStandardBindings({
      standardKind: 'data_element',
      status: 'active',
      page: 2,
      pageSize: 20,
    });

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/standard-bindings`, {
      params: {
        standard_kind: 'data_element',
        status: 'active',
        page: 2,
        page_size: 20,
      },
    });
    expect(result.total).toBe(1);
    expect(result.items[0].standardCode).toBe('customer.name');
    expect(result.page).toBe(2);
  });

  it('createStandardBinding POST，body 只带调用方给出的键', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeBindingRow());

    await createStandardBinding({
      standardKind: 'data_element',
      standardCode: 'customer.name',
      assetType: 'column',
      assetId: 'ds:dbo.t.c',
      note: '人工确认',
      source: 'suggested',
      confidence: 0.7,
    });

    expect(apiClient.post).toHaveBeenCalledWith(
      `${BASE}/standard-bindings`,
      {
        standard_kind: 'data_element',
        standard_code: 'customer.name',
        asset_type: 'column',
        asset_id: 'ds:dbo.t.c',
        note: '人工确认',
        source: 'suggested',
        confidence: 0.7,
      },
    );
  });

  it('createStandardBinding 可选键缺省时不发', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeBindingRow());

    await createStandardBinding({
      standardKind: 'metric_card',
      standardCode: 'order.count',
      assetType: 'metric',
      assetId: 'kb1:order_count',
    });

    const body = vi.mocked(apiClient.post).mock.calls[0][1] as Record<
      string,
      unknown
    >;
    expect(Object.keys(body)).toEqual([
      'standard_kind',
      'standard_code',
      'asset_type',
      'asset_id',
    ]);
  });

  it('updateStandardBinding PUT 路径编码，body 带 expected_revision', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(makeBindingRow({
      status: 'disabled',
    }));

    const result = await updateStandardBinding('b/1', {
      expectedRevision: 3,
      status: 'disabled',
    });

    expect(apiClient.put).toHaveBeenCalledWith(
      `${BASE}/standard-bindings/b%2F1`,
      { expected_revision: 3, status: 'disabled' },
    );
    expect(result.status).toBe('disabled');
  });

  it('deleteStandardBinding DELETE 路径编码', async () => {
    vi.mocked(apiClient.delete).mockResolvedValue(undefined);
    await deleteStandardBinding('b/1');
    expect(apiClient.delete).toHaveBeenCalledWith(
      `${BASE}/standard-bindings/b%2F1`,
    );
  });

  it('getStandardBindingCoverage GET coverage，coverage 保持 null', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [{
        standard_asset_id: 'sa1',
        standard_kind: 'data_element',
        standard_code: 'customer.name',
        name: '客户姓名',
        version: 1,
        domain: null,
        status: 'published',
        bindings_count: 0,
        bound_assets: [],
      }],
      total: 1,
      page: 1,
      page_size: 20,
      summary: {
        total_standards: 1,
        bound_standards: 0,
        binding_count: 0,
        coverage: null,
      },
    });

    const result = await getStandardBindingCoverage({
      kind: 'data_element',
      page: 1,
      pageSize: 20,
    });

    expect(apiClient.get).toHaveBeenCalledWith(
      `${BASE}/standard-bindings/coverage`,
      { params: { kind: 'data_element', page: 1, page_size: 20 } },
    );
    expect(result.items[0].unbound).toBe(true);
    expect(result.summary.coverage).toBeNull();
    expect(result.summary.totalStandards).toBe(1);
  });

  it('suggestStandardBindings POST suggest，条件走 query', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      items: [{
        standard_kind: 'data_element',
        standard_code: 'customer.name',
        standard_name: '客户姓名',
        name: '客户姓名',
        asset_type: 'column',
        asset_id: 'ds:dbo.t.c',
        asset_name: 'c',
        confidence: 1,
        reason: 'exact',
      }],
    });

    const result = await suggestStandardBindings({
      standardKind: 'data_element',
      limit: 10,
    });

    expect(apiClient.post).toHaveBeenCalledWith(
      `${BASE}/standard-bindings/suggest`,
      undefined,
      { params: { standard_kind: 'data_element', limit: 10 } },
    );
    expect(result[0].standardName).toBe('客户姓名');
    expect(result[0].confidence).toBe(1);
    expect(result[0].reason).toBe('exact');
  });

  it('getSourceAnchors GET source-anchors', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      asset: { asset_type: 'table', asset_id: 'ds:dbo.orders' },
      linked: [{
        knowledge_base_id: 'kb1',
        doc_type: '订单',
        entity_name: 'Orders',
        entity_count: 1,
        truncated: false,
        attributes: {},
        documents: [{ id: 'd1', name: '订单.pdf', status: 'ready' }],
      }],
      unresolved: false,
      degraded: false,
    });

    const result = await getSourceAnchors('table', 'ds:dbo.orders');

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/source-anchors`, {
      params: { asset_type: 'table', asset_id: 'ds:dbo.orders' },
    });
    expect(result.linked).toHaveLength(1);
    expect(result.linked[0].documents[0].id).toBe('d1');
    expect(result.unresolved).toBe(false);
    expect(result.degraded).toBe(false);
  });
});
