/**
 * `dataContracts` 取数模块 —— 纯函数映射/组装 + 11 个合同接口 + 数据源下拉。
 *
 * 断言钉住：
 *   ① 列表参数 camelCase → snake_case：空串丢弃、includeDrafts 仅 true 发送、分页归一；
 *   ② 草稿/更新/导入 body 只发调用方给出的键（camel → snake、yml 归一 yaml）；
 *   ③ 响应行 snake → camel、null 容错、payload 非对象 → `{}`；
 *   ④ 后端显式 `has_draft=false` **不被版本关系反推**（缺失时才推导）；
 *   ⑤ 脚手架 stats 四个计数 snake → camel、warnings 过滤非字符串；
 *   ⑥ 11 个接口的 method/路径/params/body 与导出结果、数据源过滤。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from './request';
import {
  buildDataContractDraftBody,
  buildDataContractImportBody,
  buildDataContractListParams,
  buildDataContractUpdateBody,
  createDataContractDraft,
  deleteDataContractDraft,
  exportDataContract,
  getDataContract,
  importDataContract,
  listContractDataSources,
  listDataContractVersions,
  listDataContracts,
  mapDataContractDetail,
  mapDataContractItem,
  mapScaffoldResult,
  previewDataContractScaffold,
  publishDataContract,
  retireDataContract,
  updateDataContractDraft,
  type DataContractRowRaw,
} from './dataContracts';

vi.mock('./request', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

const BASE = '/data-governance/data-contracts';

function makeRow(
  overrides: Partial<DataContractRowRaw> = {},
): DataContractRowRaw {
  return {
    contract_id: 'dc-1',
    data_source_id: 'ds-1',
    data_source_name: '营销库',
    name: '营销库合同',
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

describe('buildDataContractListParams', () => {
  it('camelCase → snake_case 并保留分页', () => {
    expect(
      buildDataContractListParams({
        status: 'published',
        keyword: '  营销  ',
        includeDrafts: true,
        page: 2,
        pageSize: 20,
      }),
    ).toEqual({
      status: 'published',
      keyword: '营销',
      include_drafts: true,
      page: 2,
      page_size: 20,
    });
  });

  it('空字符串丢弃、includeDrafts 只在 true 时发送', () => {
    expect(
      buildDataContractListParams({
        status: '',
        keyword: '   ',
        includeDrafts: false,
        page: 1,
        pageSize: 20,
      }),
    ).toEqual({ page: 1, page_size: 20 });
    expect(
      buildDataContractListParams({
        includeDrafts: undefined,
        page: 1,
        pageSize: 20,
      }),
    ).toEqual({ page: 1, page_size: 20 });
  });

  it('page 夹到 >=1，pageSize 夹到 1..100', () => {
    expect(buildDataContractListParams({ page: 0, pageSize: 0 })).toEqual({
      page: 1,
      page_size: 1,
    });
    expect(buildDataContractListParams({ page: -5, pageSize: 500 })).toEqual({
      page: 1,
      page_size: 100,
    });
    expect(
      buildDataContractListParams({ page: Number.NaN, pageSize: Number.NaN }),
    ).toEqual({ page: 1, page_size: 1 });
    expect(
      buildDataContractListParams({ page: 2.9, pageSize: 20.7 }),
    ).toEqual({ page: 2, page_size: 20 });
  });
});

describe('buildDataContractDraftBody / buildDataContractUpdateBody', () => {
  it('新建草稿：camelCase 入参转 snake_case，未传可选字段不出现', () => {
    const minimal = buildDataContractDraftBody({ dataSourceId: 'ds-1' });
    expect(minimal).toEqual({ data_source_id: 'ds-1' });
    expect(Object.keys(minimal)).toEqual(['data_source_id']);

    expect(
      buildDataContractDraftBody({
        dataSourceId: 'ds-1',
        payload: { apiVersion: 'v3.0.0' },
        name: '营销库合同',
        baseVersion: 2,
      }),
    ).toEqual({
      data_source_id: 'ds-1',
      payload: { apiVersion: 'v3.0.0' },
      name: '营销库合同',
      base_version: 2,
    });
  });

  it('更新草稿：expected_revision 必传，name/payload 仅在给出时发送', () => {
    const minimal = buildDataContractUpdateBody({ expectedRevision: 3 });
    expect(minimal).toEqual({ expected_revision: 3 });
    expect(Object.keys(minimal)).toEqual(['expected_revision']);

    expect(
      buildDataContractUpdateBody({
        expectedRevision: 3,
        name: '订单合同',
        payload: { schema: [] },
      }),
    ).toEqual({
      expected_revision: 3,
      name: '订单合同',
      payload: { schema: [] },
    });
  });

  it('导入：format 为空不发送，yml 归一为 yaml', () => {
    const minimal = buildDataContractImportBody({
      dataSourceId: 'ds-1',
      content: '{}',
    });
    expect(minimal).toEqual({
      data_source_id: 'ds-1',
      content: '{}',
    });
    expect(Object.keys(minimal).sort()).toEqual(['content', 'data_source_id']);

    expect(
      buildDataContractImportBody({
        dataSourceId: 'ds-1',
        content: 'kind: DataContract',
        // 运行时防御：调用方可能从输入框拿到大小写/别名形式。
        format: ' YML ' as unknown as 'yaml',
      }),
    ).toEqual({
      data_source_id: 'ds-1',
      content: 'kind: DataContract',
      format: 'yaml',
    });
    expect(
      buildDataContractImportBody({
        dataSourceId: 'ds-1',
        content: '{}',
        format: '',
      }),
    ).toEqual({ data_source_id: 'ds-1', content: '{}' });
  });
});

describe('mapDataContractItem', () => {
  it('snake_case → camelCase 全字段映射', () => {
    expect(mapDataContractItem(makeRow())).toEqual({
      contractId: 'dc-1',
      dataSourceId: 'ds-1',
      dataSourceName: '营销库',
      name: '营销库合同',
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
      mapDataContractItem({
        contract_id: 'dc-2',
        data_source_id: 'ds-2',
        name: '合同',
        version: 1,
        status: 'draft',
        revision: 1,
      }),
    ).toEqual({
      contractId: 'dc-2',
      dataSourceId: 'ds-2',
      dataSourceName: null,
      name: '合同',
      version: 1,
      status: 'draft',
      revision: 1,
      publishedVersion: null,
      draftVersion: null,
      hasDraft: false,
      updatedAt: null,
      updatedBy: null,
    });
  });

  it('显式 null 与缺失同样处理（非数字版本也不透传）', () => {
    const mapped = mapDataContractItem(
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

    const dirty = mapDataContractItem(
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
    expect(mapDataContractItem(withoutHasDraft).hasDraft).toBe(true);
    expect(mapDataContractItem(makeRow({ has_draft: null })).hasDraft).toBe(
      true,
    );
  });

  it('后端显式 has_draft=false 不被「草稿版本更高」反推成 true', () => {
    const mapped = mapDataContractItem(
      makeRow({ published_version: 2, draft_version: 3, has_draft: false }),
    );
    expect(mapped.draftVersion).toBe(3);
    expect(mapped.publishedVersion).toBe(2);
    expect(mapped.hasDraft).toBe(false);
  });
});

describe('mapDataContractDetail', () => {
  it('在列表字段之外保留 payload 对象', () => {
    const mapped = mapDataContractDetail(
      makeRow({ payload: { apiVersion: 'v3.0.0', schema: [] } }),
    );
    expect(mapped.contractId).toBe('dc-1');
    expect(mapped.hasDraft).toBe(true);
    expect(mapped.payload).toEqual({ apiVersion: 'v3.0.0', schema: [] });
  });

  it('payload 缺失或非对象时回退为空对象', () => {
    expect(mapDataContractDetail(makeRow()).payload).toEqual({});
    expect(mapDataContractDetail(makeRow({ payload: null })).payload).toEqual(
      {},
    );
    expect(mapDataContractDetail(makeRow({ payload: 'oops' })).payload).toEqual(
      {},
    );
    expect(
      mapDataContractDetail(makeRow({ payload: ['oops'] })).payload,
    ).toEqual({});
  });
});

describe('mapScaffoldResult', () => {
  it('stats snake → camel、schemaVersion 透传、warnings 只保留字符串', () => {
    expect(
      mapScaffoldResult({
        data_source_id: 'ds-1',
        schema_version: 7,
        payload: { apiVersion: 'v3.0.0' },
        stats: {
          tables: 3,
          columns: 12,
          skipped_tables: 1,
          skipped_columns: 2,
        },
        warnings: ['表 a 跳过', '', 42 as unknown as string, '列 b 跳过'],
      }),
    ).toEqual({
      dataSourceId: 'ds-1',
      schemaVersion: 7,
      payload: { apiVersion: 'v3.0.0' },
      stats: {
        tables: 3,
        columns: 12,
        skippedTables: 1,
        skippedColumns: 2,
      },
      warnings: ['表 a 跳过', '列 b 跳过'],
    });
  });

  it('stats/warnings/payload 缺失与非数字都兜底', () => {
    const mapped = mapScaffoldResult({
      data_source_id: null,
      schema_version: null,
      stats: {
        tables: -1,
        columns: '3' as unknown as number,
        skipped_tables: Number.NaN,
        skipped_columns: 2.9,
      },
      warnings: 'oops' as unknown as string[],
    });
    expect(mapped).toEqual({
      dataSourceId: '',
      schemaVersion: null,
      payload: {},
      stats: {
        tables: 0,
        columns: 0,
        skippedTables: 0,
        skippedColumns: 2,
      },
      warnings: [],
    });
    expect(mapScaffoldResult({}).stats).toEqual({
      tables: 0,
      columns: 0,
      skippedTables: 0,
      skippedColumns: 0,
    });
  });
});

describe('接口封装', () => {
  it('listDataContracts 走列表端点，按参数组装传参并映射 items/total', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [makeRow({ has_draft: false })],
      total: 7,
    });

    const result = await listDataContracts({
      keyword: ' 营销 ',
      includeDrafts: true,
      page: 3,
      pageSize: 50,
    });

    expect(apiClient.get).toHaveBeenCalledTimes(1);
    expect(apiClient.get).toHaveBeenCalledWith(BASE, {
      params: {
        keyword: '营销',
        include_drafts: true,
        page: 3,
        page_size: 50,
      },
    });
    expect(result.total).toBe(7);
    expect(result.items).toHaveLength(1);
    expect(result.items[0].contractId).toBe('dc-1');
    expect(result.items[0].hasDraft).toBe(false);
  });

  it('listDataContracts 空响应兜底为空列表/0', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({});

    await expect(
      listDataContracts({ page: 1, pageSize: 20 }),
    ).resolves.toEqual({ items: [], total: 0 });
  });

  it('getDataContract GET 详情路径（id 编码）并映射 payload', async () => {
    vi.mocked(apiClient.get).mockResolvedValue(
      makeRow({ payload: { apiVersion: 'v3.0.0' } }),
    );

    const detail = await getDataContract('dc/1');

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/dc%2F1`);
    expect(detail.payload).toEqual({ apiVersion: 'v3.0.0' });
  });

  it('listDataContractVersions GET versions，data_source_id 走查询参数', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ items: [makeRow()] });

    const versions = await listDataContractVersions('ds/1');

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/versions`, {
      params: { data_source_id: 'ds/1' },
    });
    expect(versions).toHaveLength(1);
    expect(versions[0].contractId).toBe('dc-1');
    expect(versions[0].payload).toEqual({});
  });

  it('previewDataContractScaffold POST scaffold-preview，body 带 data_source_id', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      data_source_id: 'ds-1',
      schema_version: 4,
      payload: { apiVersion: 'v3.0.0' },
      stats: {
        tables: 2,
        columns: 5,
        skipped_tables: 0,
        skipped_columns: 1,
      },
      warnings: ['列 a 跳过'],
    });

    const result = await previewDataContractScaffold('ds-1');

    expect(apiClient.post).toHaveBeenCalledWith(`${BASE}/scaffold-preview`, {
      data_source_id: 'ds-1',
    });
    expect(result.stats).toEqual({
      tables: 2,
      columns: 5,
      skippedTables: 0,
      skippedColumns: 1,
    });
    expect(result.schemaVersion).toBe(4);
    expect(result.warnings).toEqual(['列 a 跳过']);
  });

  it('createDataContractDraft POST drafts，body 为 snake_case 组装结果', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeRow());

    await createDataContractDraft({
      dataSourceId: 'ds-1',
      payload: { apiVersion: 'v3.0.0' },
      name: '营销库合同',
      baseVersion: 2,
    });

    expect(apiClient.post).toHaveBeenCalledWith(`${BASE}/drafts`, {
      data_source_id: 'ds-1',
      payload: { apiVersion: 'v3.0.0' },
      name: '营销库合同',
      base_version: 2,
    });
  });

  it('updateDataContractDraft PUT 详情路径，body 带 expected_revision', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(makeRow());

    await updateDataContractDraft('dc-1', {
      expectedRevision: 3,
      name: '订单合同',
    });

    expect(apiClient.put).toHaveBeenCalledWith(`${BASE}/dc-1`, {
      expected_revision: 3,
      name: '订单合同',
    });
  });

  it('publishDataContract POST publish，body 只带 expected_revision', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeRow());

    await publishDataContract('dc-1', 5);

    expect(apiClient.post).toHaveBeenCalledWith(`${BASE}/dc-1/publish`, {
      expected_revision: 5,
    });
    const body = vi.mocked(apiClient.post).mock.calls[0][1] as Record<
      string,
      unknown
    >;
    expect(Object.keys(body)).toEqual(['expected_revision']);
  });

  it('retireDataContract POST retire（无 body）', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeRow());

    await retireDataContract('dc-1');

    expect(apiClient.post).toHaveBeenCalledWith(`${BASE}/dc-1/retire`);
  });

  it('deleteDataContractDraft DELETE 详情路径', async () => {
    vi.mocked(apiClient.delete).mockResolvedValue(undefined);

    await deleteDataContractDraft('dc/1');

    expect(apiClient.delete).toHaveBeenCalledTimes(1);
    expect(apiClient.delete).toHaveBeenCalledWith(`${BASE}/dc%2F1`);
  });

  it('importDataContract POST import，content 原样、format 归一', async () => {
    vi.mocked(apiClient.post).mockResolvedValue(makeRow());

    await importDataContract({
      dataSourceId: 'ds-1',
      content: 'kind: DataContract',
      format: 'YML' as unknown as 'yaml',
    });

    expect(apiClient.post).toHaveBeenCalledWith(`${BASE}/import`, {
      data_source_id: 'ds-1',
      content: 'kind: DataContract',
      format: 'yaml',
    });
  });

  it('exportDataContract GET export，缺省 format=json 并原样回传内容', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      content: '{\n  "apiVersion": "v3.0.0"\n}',
      format: 'json',
      filename: 'ds-1-2.json',
    });

    const result = await exportDataContract('dc/1');

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/dc%2F1/export`, {
      params: { format: 'json' },
    });
    expect(result).toEqual({
      content: '{\n  "apiVersion": "v3.0.0"\n}',
      format: 'json',
      filename: 'ds-1-2.json',
    });
  });

  it('exportDataContract 显式 yaml 并容错缺失字段', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      content: 'kind: DataContract',
      format: 'yaml',
    });

    const result = await exportDataContract('dc-1', 'yaml');

    expect(apiClient.get).toHaveBeenCalledWith(`${BASE}/dc-1/export`, {
      params: { format: 'yaml' },
    });
    expect(result.filename).toBe('');
  });

  it('listContractDataSources GET data-sources 并只保留 database 源', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [
        { id: 'ds-db', name: '营销库', access_type: 'database' },
        { id: 'ds-api', name: 'API 源', access_type: 'api' },
        { id: 'ds-file', name: '文件源', access_type: 'file' },
        { id: '', name: '坏数据', access_type: 'database' },
      ],
      total: 4,
    });

    const options = await listContractDataSources('kb-1');

    expect(apiClient.get).toHaveBeenCalledWith(
      '/knowledge-base/data-sources',
      { params: { knowledge_base_id: 'kb-1' } },
    );
    expect(options).toEqual([
      { id: 'ds-db', name: '营销库', accessType: 'database' },
    ]);
  });

  it('listContractDataSources 空响应兜底为空数组', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({});

    await expect(listContractDataSources('kb-1')).resolves.toEqual([]);
  });
});
