/**
 * `standardAssets` 取数模块 —— 纯函数映射/组装 + 8 个接口封装。
 *
 * 断言钉住：
 *   ① 列表参数 camelCase → snake_case：空串丢弃、includeDrafts 仅 true 发送、分页归一；
 *   ② 草稿 body 只发调用方给出的键（camel → snake）；
 *   ③ 响应行 snake → camel、null 容错、payload 非对象 → `{}`；
 *   ④ 后端显式 `has_draft=false` **不被版本关系反推**（缺失时才推导）；
 *   ⑤ 8 个 async 封装的路径与 method/params/body。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from './request';
import {
  buildDraftBody,
  buildStandardAssetListParams,
  buildUpdateBody,
  createStandardDraft,
  deleteStandardDraft,
  getStandardAsset,
  listStandardAssetVersions,
  listStandardAssets,
  mapStandardAssetDetail,
  mapStandardAssetItem,
  publishStandardAsset,
  retireStandardAsset,
  updateStandardDraft,
  type StandardAssetRowRaw,
} from './standardAssets';

vi.mock('./request', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

const BASE = '/data-governance/standard-assets';

function makeRow(
  overrides: Partial<StandardAssetRowRaw> = {},
): StandardAssetRowRaw {
  return {
    asset_id: 'sa-1',
    kind: 'data_element',
    code: 'customer.name',
    name: '客户姓名',
    version: 2,
    status: 'published',
    revision: 3,
    published_version: 2,
    draft_version: 3,
    has_draft: true,
    updated_at: '2026-10-19T08:00:00Z',
    updated_by: 'user-1',
    ...overrides,
  };
}

beforeEach(() => {
  // reset（而非 clear）：连同 mock 实现一起清掉，避免用例间串用返回值。
  vi.resetAllMocks();
});

describe('buildStandardAssetListParams', () => {
  it('camelCase → snake_case 并保留分页', () => {
    expect(
      buildStandardAssetListParams({
        kind: 'term',
        status: 'published',
        keyword: '  客户  ',
        includeDrafts: true,
        page: 2,
        pageSize: 20,
      }),
    ).toEqual({
      kind: 'term',
      status: 'published',
      keyword: '客户',
      include_drafts: true,
      page: 2,
      page_size: 20,
    });
  });

  it('空字符串丢弃、includeDrafts 只在 true 时发送', () => {
    expect(
      buildStandardAssetListParams({
        kind: '',
        status: '',
        keyword: '   ',
        includeDrafts: false,
        page: 1,
        pageSize: 20,
      }),
    ).toEqual({ page: 1, page_size: 20 });
    expect(
      buildStandardAssetListParams({
        includeDrafts: undefined,
        page: 1,
        pageSize: 20,
      }),
    ).toEqual({ page: 1, page_size: 20 });
  });

  it('page 夹到 >=1，pageSize 夹到 1..100', () => {
    expect(buildStandardAssetListParams({ page: 0, pageSize: 0 })).toEqual({
      page: 1,
      page_size: 1,
    });
    expect(buildStandardAssetListParams({ page: -5, pageSize: 500 })).toEqual({
      page: 1,
      page_size: 100,
    });
    expect(
      buildStandardAssetListParams({ page: Number.NaN, pageSize: Number.NaN }),
    ).toEqual({ page: 1, page_size: 1 });
    expect(
      buildStandardAssetListParams({ page: 2.9, pageSize: 20.7 }),
    ).toEqual({ page: 2, page_size: 20 });
  });
});

describe('buildDraftBody / buildUpdateBody', () => {
  it('新建草稿：camelCase 入参转 snake_case，未传可选字段不出现', () => {
    const minimal = buildDraftBody({ kind: 'term', code: 'order', name: '订单' });
    expect(minimal).toEqual({ kind: 'term', code: 'order', name: '订单' });
    expect(Object.keys(minimal).sort()).toEqual(['code', 'kind', 'name']);

    expect(
      buildDraftBody({
        kind: 'data_element',
        code: 'customer.name',
        name: '客户姓名',
        payload: { definition: 'x' },
        baseVersion: 2,
      }),
    ).toEqual({
      kind: 'data_element',
      code: 'customer.name',
      name: '客户姓名',
      payload: { definition: 'x' },
      base_version: 2,
    });
  });

  it('更新草稿：expected_revision 必传，name/payload 仅在给出时发送', () => {
    const minimal = buildUpdateBody({ expectedRevision: 3 });
    expect(minimal).toEqual({ expected_revision: 3 });
    expect(Object.keys(minimal)).toEqual(['expected_revision']);

    expect(
      buildUpdateBody({
        expectedRevision: 3,
        name: '订单',
        payload: { definition: 'x' },
      }),
    ).toEqual({
      expected_revision: 3,
      name: '订单',
      payload: { definition: 'x' },
    });
  });
});

describe('mapStandardAssetItem', () => {
  it('snake_case → camelCase 全字段映射', () => {
    expect(mapStandardAssetItem(makeRow())).toEqual({
      assetId: 'sa-1',
      kind: 'data_element',
      code: 'customer.name',
      name: '客户姓名',
      version: 2,
      status: 'published',
      revision: 3,
      publishedVersion: 2,
      draftVersion: 3,
      hasDraft: true,
      updatedAt: '2026-10-19T08:00:00Z',
      updatedBy: 'user-1',
    });
  });

  it('可空字段缺失时给出安全默认值', () => {
    expect(
      mapStandardAssetItem({
        asset_id: 'sa-2',
        kind: 'term',
        code: 'order',
        name: '订单',
        version: 1,
        status: 'published',
        revision: 1,
      }),
    ).toEqual({
      assetId: 'sa-2',
      kind: 'term',
      code: 'order',
      name: '订单',
      version: 1,
      status: 'published',
      revision: 1,
      publishedVersion: null,
      draftVersion: null,
      hasDraft: false,
      updatedAt: null,
      updatedBy: null,
    });
  });

  it('显式 null 与缺失同样处理（非数字版本也不透传）', () => {
    const mapped = mapStandardAssetItem(
      makeRow({
        published_version: null,
        draft_version: null,
        has_draft: null,
        updated_at: null,
        updated_by: null,
      }),
    );
    expect(mapped.publishedVersion).toBeNull();
    expect(mapped.draftVersion).toBeNull();
    expect(mapped.hasDraft).toBe(false);
    expect(mapped.updatedAt).toBeNull();
    expect(mapped.updatedBy).toBeNull();

    const dirty = mapStandardAssetItem(
      makeRow({
        version: 'oops' as unknown as number,
        revision: undefined as unknown as number,
        published_version: '3' as unknown as number,
        draft_version: Number.POSITIVE_INFINITY,
      }),
    );
    expect(dirty.version).toBe(0);
    expect(dirty.revision).toBe(1);
    expect(dirty.publishedVersion).toBeNull();
    expect(dirty.draftVersion).toBeNull();
  });

  it('has_draft 缺失或 null 时才按版本关系推导', () => {
    const { has_draft: _ignored, ...withoutHasDraft } = makeRow();
    expect(mapStandardAssetItem(withoutHasDraft).hasDraft).toBe(true);
    expect(mapStandardAssetItem(makeRow({ has_draft: null })).hasDraft).toBe(true);
  });

  it('后端显式 has_draft=false 不被「草稿版本更高」反推成 true', () => {
    const mapped = mapStandardAssetItem(
      makeRow({ published_version: 2, draft_version: 3, has_draft: false }),
    );
    expect(mapped.draftVersion).toBe(3);
    expect(mapped.publishedVersion).toBe(2);
    expect(mapped.hasDraft).toBe(false);
  });
});

describe('mapStandardAssetDetail', () => {
  it('在列表字段之外保留 payload 对象', () => {
    const mapped = mapStandardAssetDetail(
      makeRow({ payload: { definition: '客户姓名', data_type: 'string' } }),
    );
    expect(mapped.assetId).toBe('sa-1');
    expect(mapped.hasDraft).toBe(true);
    expect(mapped.payload).toEqual({
      definition: '客户姓名',
      data_type: 'string',
    });
  });

  it('payload 缺失或非对象时回退为空对象', () => {
    expect(mapStandardAssetDetail(makeRow()).payload).toEqual({});
    expect(mapStandardAssetDetail(makeRow({ payload: null })).payload).toEqual({});
    expect(mapStandardAssetDetail(makeRow({ payload: 'oops' })).payload).toEqual(
      {},
    );
    expect(mapStandardAssetDetail(makeRow({ payload: ['oops'] })).payload).toEqual(
      {},
    );
  });
});

describe('接口封装', () => {
  it('listStandardAssets 走列表端点，按参数组装传参并映射 items/total', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [makeRow({ has_draft: false })],
      total: 7,
    });

    const result = await listStandardAssets({
      kind: 'data_element',
      keyword: ' 客户 ',
      includeDrafts: true,
      page: 3,
      pageSize: 50,
    });

    expect(apiClient.get).toHaveBeenCalledTimes(1);
    expect(apiClient.get).toHaveBeenCalledWith(BASE, {
      params: {
        kind: 'data_element',
        keyword: '客户',
        include_drafts: true,
        page: 3,
        page_size: 50,
      },
    });
    expect(result.total).toBe(7);
    expect(result.items).toHaveLength(1);
    expect(result.items[0].assetId).toBe('sa-1');
    expect(result.items[0].hasDraft).toBe(false);
  });

  it('listStandardAssets 空响应兜底为空列表/0', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({});

    await expect(
      listStandardAssets({ page: 1, pageSize: 20 }),
    ).resolves.toEqual({ items: [], total: 0 });
  });

  it('getStandardAsset GET 详情路径（id 编码）并映射 payload', async () => {
    vi.mocked(apiClient.get).mockResolvedValue(
      makeRow({ payload: { definition: 'x' } }),
    );

    const detail = await getStandardAsset('sa/1');

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/sa%2F1`);
    expect(detail.payload).toEqual({ definition: 'x' });
  });

  it('listStandardAssetVersions GET versions，kind/code 走查询参数', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ items: [makeRow()] });

    const versions = await listStandardAssetVersions('term', 'order');

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/versions`, {
      params: { kind: 'term', code: 'order' },
    });
    expect(versions).toHaveLength(1);
    expect(versions[0].assetId).toBe('sa-1');
  });

  it('createStandardDraft POST drafts，body 为 snake_case 组装结果', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeRow());

    await createStandardDraft({
      kind: 'data_element',
      code: 'customer.name',
      name: '客户姓名',
      payload: { definition: 'x' },
      baseVersion: 2,
    });

    expect(apiClient.post).toHaveBeenCalledWith(`${BASE}/drafts`, {
      kind: 'data_element',
      code: 'customer.name',
      name: '客户姓名',
      payload: { definition: 'x' },
      base_version: 2,
    });
  });

  it('updateStandardDraft PUT 详情路径，body 带 expected_revision', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(makeRow());

    await updateStandardDraft('sa-1', { expectedRevision: 3, name: '订单' });

    expect(apiClient.put).toHaveBeenCalledWith(`${BASE}/sa-1`, {
      expected_revision: 3,
      name: '订单',
    });
  });

  it('publishStandardAsset POST publish，body 只带 expected_revision', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeRow());

    await publishStandardAsset('sa-1', 5);

    expect(apiClient.post).toHaveBeenCalledWith(`${BASE}/sa-1/publish`, {
      expected_revision: 5,
    });
    const body = vi.mocked(apiClient.post).mock.calls[0][1] as Record<
      string,
      unknown
    >;
    expect(Object.keys(body)).toEqual(['expected_revision']);
  });

  it('retireStandardAsset POST retire（无 body）', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeRow());

    await retireStandardAsset('sa-1');

    expect(apiClient.post).toHaveBeenCalledWith(`${BASE}/sa-1/retire`);
  });

  it('deleteStandardDraft DELETE 详情路径', async () => {
    vi.mocked(apiClient.delete).mockResolvedValue(undefined);

    await deleteStandardDraft('sa/1');

    expect(apiClient.delete).toHaveBeenCalledTimes(1);
    expect(apiClient.delete).toHaveBeenCalledWith(`${BASE}/sa%2F1`);
  });
});
