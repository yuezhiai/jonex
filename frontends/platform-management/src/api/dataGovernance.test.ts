/**
 * `dataGovernance` 取数模块 —— 纯函数映射 + 接口封装。
 *
 * 断言钉住四件事：
 *   ① 查询参数 camelCase → snake_case，空/空白不传、assetType 缺省 all、pageSize 夹 1..100；
 *   ② `quality_verdict` / `sensitivity` 的 null **原样保留**（null 是「未知」，不能伪造成 ok）；
 *   ③ 详情保留 quality.reasons 与文档/数据源两套专属字段；
 *   ④ 血缘 nodes/edges/counts/truncated 映射，`counts.chunks` 可为 null。
 *
 * 另加一组接口封装断言：`updateAssetMetadata` 的 body 只能是调用方给的 patch
 * （不得混入 asset_type / asset_id / tenant_id），其余接口的路径与参数口径对齐后端。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from './request';
import {
  archiveAsset,
  batchUpdateClassification,
  buildListParams,
  destroyAsset,
  getAsset,
  getAssetLineage,
  getClassificationDefault,
  getRetentionPolicy,
  listAssets,
  listClassificationConfig,
  mapAssetDetail,
  mapAssetItem,
  mapLineage,
  restoreAsset,
  setClassificationDefault,
  setRetentionPolicy,
  updateAssetMetadata,
  upsertClassificationCategory,
  upsertClassificationLevel,
} from './dataGovernance';
import type { DgAssetListParams } from '@/pages/DataGovernance/types';

vi.mock('./request', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

type ItemRow = Parameters<typeof mapAssetItem>[0];
type DetailRow = Parameters<typeof mapAssetDetail>[0];
type LineageRow = Parameters<typeof mapLineage>[0];

function makeItemRow(overrides: Partial<ItemRow> = {}): ItemRow {
  return {
    asset_type: 'document',
    asset_id: 'doc-1',
    name: '季度报告.pdf',
    kb_id: 'kb-1',
    kb_name: '财务知识库',
    space_id: 'space-1',
    status: 'ready',
    updated_at: '2026-10-18T08:00:00Z',
    owner: null,
    domain: null,
    sensitivity: null,
    quality_verdict: null,
    classification_categories: [],
    classification_sources: { sensitivity: 'none', classification_categories: 'none' },
    ...overrides,
  };
}

function makeDetailRow(overrides: Partial<DetailRow> = {}): DetailRow {
  return {
    ...makeItemRow(),
    file_size: 1024,
    mime_type: 'application/pdf',
    data_source_type: null,
    folder_id: 'folder-1',
    content_generation: 3,
    ontology_status: 'ready',
    access_type: null,
    sync_mode: null,
    last_sync_at: null,
    last_sync_status: null,
    document_count: null,
    quality: { verdict: 'bad', reasons: ['missing_body'] },
    created_at: '2026-09-01T00:00:00Z',
    archived_at: null,
    retention_policy: null,
    ...overrides,
  };
}

/** 生效保留策略行（snake_case，后端 `retention_policy` 投影形状）。 */
function makeRetentionRow() {
  return {
    scope: 'kb' as const,
    effective_from: 'kb' as const,
    retention_days: 180,
    grace_days: 14,
    sources: { retention_days: 'kb' as const, grace_days: 'tenant' as const },
  };
}

function makeLineageRow(overrides: Partial<LineageRow> = {}): LineageRow {
  return {
    asset: { asset_type: 'document', asset_id: 'doc-1', name: '季度报告.pdf' },
    nodes: [
      {
        id: 'document:doc-1',
        type: 'document',
        label: '季度报告.pdf',
        meta: { kb_id: 'kb-1' },
      },
      {
        id: 'entity:收入',
        type: 'ontology_entity',
        label: '收入',
        meta: { entity_type: 'Metric' },
      },
    ],
    edges: [
      {
        source: 'document:doc-1',
        target: 'entity:收入',
        type: 'produces_entity',
        meta: { entity_type: 'Metric' },
      },
    ],
    counts: { chunks: null, entities: 1, relations: 0, citations: 2 },
    truncated: { entities: false, relations: false, chunks: false },
    ...overrides,
  };
}

beforeEach(() => {
  // reset（而非 clear）：连同 mock 实现一起清掉，避免用例间串用返回值。
  vi.resetAllMocks();
});

describe('buildListParams', () => {
  it('把 camelCase 参数转成后端 snake_case，sensitivity(unclassified) 原样透传', () => {
    const params: DgAssetListParams = {
      assetType: 'data_source',
      spaceId: 'space-1',
      kbId: 'kb-1',
      domain: 'finance',
      owner: 'alice',
      sensitivity: 'unclassified',
      status: 'active',
      keyword: '合同',
      page: 3,
      pageSize: 50,
    };

    expect(buildListParams(params)).toEqual({
      asset_type: 'data_source',
      domain_space_id: 'space-1',
      kb_id: 'kb-1',
      domain: 'finance',
      owner: 'alice',
      sensitivity: 'unclassified',
      status: 'active',
      keyword: '合同',
      page: 3,
      page_size: 50,
    });
  });

  it('assetType 缺省为 all，空/空白字符串不传，非空值去空白', () => {
    expect(
      buildListParams({
        page: 1,
        pageSize: 20,
        spaceId: '  ',
        kbId: '',
        domain: '\t',
        owner: ' ',
        status: '\n',
        keyword: '   ',
      }),
    ).toEqual({
      asset_type: 'all',
      page: 1,
      page_size: 20,
    });

    expect(
      buildListParams({
        spaceId: ' space-1 ',
        kbId: ' kb-1 ',
        domain: ' finance ',
        owner: ' alice ',
        status: ' ready ',
        keyword: ' 报告 ',
        page: 1,
        pageSize: 20,
      }),
    ).toEqual({
      asset_type: 'all',
      domain_space_id: 'space-1',
      kb_id: 'kb-1',
      domain: 'finance',
      owner: 'alice',
      status: 'ready',
      keyword: '报告',
      page: 1,
      page_size: 20,
    });
  });

  it('page 取整并夹到 ≥1（与 page_size 口径对称）', () => {
    const at = (page: number) => buildListParams({ page, pageSize: 20 }).page;

    expect(at(0)).toBe(1);
    expect(at(-3)).toBe(1);
    expect(at(1)).toBe(1);
    expect(at(3.9)).toBe(3);
    expect(at(NaN)).toBe(1);
    expect(at(Infinity)).toBe(1);
    expect(at(-Infinity)).toBe(1);
  });

  it('page_size 夹取到 1..100，非有限数与小数按同一口径处理', () => {
    const at = (pageSize: number) =>
      buildListParams({ page: 1, pageSize }).page_size;

    expect(at(0)).toBe(1);
    expect(at(-5)).toBe(1);
    expect(at(1)).toBe(1);
    expect(at(20.9)).toBe(20);
    expect(at(100)).toBe(100);
    expect(at(101)).toBe(100);
    expect(at(5000)).toBe(100);
    expect(at(NaN)).toBe(1);
    expect(at(Infinity)).toBe(1);
  });

  it('category 去空白后透传，空白不传', () => {
    expect(
      buildListParams({ category: ' 个人敏感 ', page: 1, pageSize: 20 }),
    ).toEqual({
      asset_type: 'all',
      category: '个人敏感',
      page: 1,
      page_size: 20,
    });
    expect(buildListParams({ category: '   ', page: 1, pageSize: 20 })).toEqual({
      asset_type: 'all',
      page: 1,
      page_size: 20,
    });
  });
});

describe('mapAssetItem', () => {
  it('snake_case → camelCase；quality_verdict=null 与 sensitivity=null 原样保留', () => {
    const mapped = mapAssetItem(makeItemRow());

    expect(mapped).toEqual({
      assetType: 'document',
      assetId: 'doc-1',
      name: '季度报告.pdf',
      kbId: 'kb-1',
      kbName: '财务知识库',
      spaceId: 'space-1',
      status: 'ready',
      updatedAt: '2026-10-18T08:00:00Z',
      owner: null,
      domain: null,
      sensitivity: null,
      qualityVerdict: null,
      classificationCategories: [],
      classificationSources: { sensitivity: 'none', classificationCategories: 'none' },
    });
    // null 表示「质量未知」，不能被映射成 ok。
    expect(mapped.qualityVerdict).toBeNull();
    expect(mapped.sensitivity).toBeNull();
  });

  it('映射分类类目与来源；缺失时默认空数组与 none', () => {
    const mapped = mapAssetItem(
      makeItemRow({
        classification_categories: ['pii', 'financial'],
        classification_sources: {
          sensitivity: 'asset',
          classification_categories: 'kb',
        },
      }),
    );
    expect(mapped.classificationCategories).toEqual(['pii', 'financial']);
    expect(mapped.classificationSources).toEqual({
      sensitivity: 'asset',
      classificationCategories: 'kb',
    });

    const row = makeItemRow();
    delete (row as Partial<ItemRow>).classification_categories;
    delete (row as Partial<ItemRow>).classification_sources;
    expect(mapAssetItem(row)).toMatchObject({
      classificationCategories: [],
      classificationSources: { sensitivity: 'none', classificationCategories: 'none' },
    });
  });

  it('质量档位与密级有值时照实映射', () => {
    const mapped = mapAssetItem(
      makeItemRow({
        quality_verdict: 'bad',
        sensitivity: 'internal',
        owner: 'alice',
        domain: 'finance',
      }),
    );

    expect(mapped.qualityVerdict).toBe('bad');
    expect(mapped.sensitivity).toBe('internal');
    expect(mapped.owner).toBe('alice');
    expect(mapped.domain).toBe('finance');
  });
});

describe('mapAssetDetail', () => {
  it('文档详情保留 quality.reasons 与 mime_type/file_size 等专属字段', () => {
    // 后端详情把 quality_verdict 收进 quality 对象，顶层不再出现该键。
    const mapped = mapAssetDetail({ ...makeDetailRow(), quality_verdict: undefined });

    expect(mapped).toEqual({
      assetType: 'document',
      assetId: 'doc-1',
      name: '季度报告.pdf',
      kbId: 'kb-1',
      kbName: '财务知识库',
      spaceId: 'space-1',
      status: 'ready',
      updatedAt: '2026-10-18T08:00:00Z',
      owner: null,
      domain: null,
      sensitivity: null,
      qualityVerdict: 'bad',
      fileSize: 1024,
      mimeType: 'application/pdf',
      dataSourceType: null,
      folderId: 'folder-1',
      contentGeneration: 3,
      ontologyStatus: 'ready',
      accessType: null,
      syncMode: null,
      lastSyncAt: null,
      lastSyncStatus: null,
      documentCount: null,
      quality: { verdict: 'bad', reasons: ['missing_body'] },
      createdAt: '2026-09-01T00:00:00Z',
      archivedAt: null,
      retentionPolicy: null,
      classificationCategories: [],
      classificationSources: { sensitivity: 'none', classificationCategories: 'none' },
    });
  });

  it('映射 archived_at 与 retention_policy（嵌套 sources 一并转 camelCase）', () => {
    const mapped = mapAssetDetail(
      makeDetailRow({
        status: 'archived',
        archived_at: '2026-10-01T00:00:00Z',
        retention_policy: makeRetentionRow(),
      }),
    );

    expect(mapped.archivedAt).toBe('2026-10-01T00:00:00Z');
    expect(mapped.retentionPolicy).toEqual({
      scope: 'kb',
      effectiveFrom: 'kb',
      retentionDays: 180,
      graceDays: 14,
      sources: { retentionDays: 'kb', graceDays: 'tenant' },
    });
  });

  it('created_at/archived_at/retention_policy 缺失时映射为 null', () => {
    const row = makeDetailRow();
    delete (row as Partial<DetailRow>).created_at;
    delete (row as Partial<DetailRow>).archived_at;
    delete (row as Partial<DetailRow>).retention_policy;

    const mapped = mapAssetDetail(row);

    expect(mapped.createdAt).toBeNull();
    expect(mapped.archivedAt).toBeNull();
    expect(mapped.retentionPolicy).toBeNull();
  });

  it('retention_policy.sources 缺失时按 effective_from/scope 回落，不崩', () => {
    const mapped = mapAssetDetail(
      makeDetailRow({
        retention_policy: {
          scope: 'kb',
          effective_from: 'tenant',
          retention_days: 90,
          grace_days: 7,
        },
      }),
    );

    expect(mapped.retentionPolicy).toEqual({
      scope: 'kb',
      effectiveFrom: 'tenant',
      retentionDays: 90,
      graceDays: 7,
      // retentionDays 的来源优先沿用 effective_from；graceDays 无法得知，落回 scope。
      sources: { retentionDays: 'tenant', graceDays: 'kb' },
    });
  });

  it('档位嵌套优先：顶层与 quality.verdict 不一致时以嵌套为准', () => {
    const mapped = mapAssetDetail(
      makeDetailRow({
        quality_verdict: 'ok',
        quality: { verdict: 'bad', reasons: ['missing_body'] },
      }),
    );

    expect(mapped.qualityVerdict).toBe('bad');
    expect(mapped.quality.verdict).toBe('bad');
  });

  it('quality 整体缺失时兜底为「未知」，不伪造 ok', () => {
    const row = makeDetailRow();
    delete row.quality;

    const mapped = mapAssetDetail(row);

    expect(mapped.qualityVerdict).toBeNull();
    expect(mapped.quality).toEqual({ verdict: null, reasons: [] });
  });

  it('数据源详情映射 access_type/sync_mode 等字段，quality.reasons 为空数组也保留', () => {
    const mapped = mapAssetDetail(
      makeDetailRow({
        asset_type: 'data_source',
        asset_id: 'ds-1',
        name: 'ESWiki',
        status: 'active',
        file_size: null,
        mime_type: null,
        data_source_type: null,
        folder_id: null,
        content_generation: null,
        ontology_status: null,
        access_type: 'api',
        sync_mode: 'manual',
        last_sync_at: '2026-10-18T07:30:00Z',
        last_sync_status: 'success',
        document_count: 12,
        quality: { verdict: null, reasons: [] },
      }),
    );

    expect(mapped.accessType).toBe('api');
    expect(mapped.syncMode).toBe('manual');
    expect(mapped.lastSyncAt).toBe('2026-10-18T07:30:00Z');
    expect(mapped.lastSyncStatus).toBe('success');
    expect(mapped.documentCount).toBe(12);
    expect(mapped.fileSize).toBeNull();
    expect(mapped.mimeType).toBeNull();
    expect(mapped.qualityVerdict).toBeNull();
    expect(mapped.quality).toEqual({ verdict: null, reasons: [] });
  });
});

describe('mapLineage', () => {
  it('映射 asset/nodes/edges/counts/truncated，counts.chunks 允许为 null', () => {
    expect(mapLineage(makeLineageRow())).toEqual({
      asset: { assetType: 'document', assetId: 'doc-1', name: '季度报告.pdf' },
      nodes: [
        {
          id: 'document:doc-1',
          type: 'document',
          label: '季度报告.pdf',
          meta: { kb_id: 'kb-1' },
        },
        {
          id: 'entity:收入',
          type: 'ontology_entity',
          label: '收入',
          meta: { entity_type: 'Metric' },
        },
      ],
      edges: [
        {
          source: 'document:doc-1',
          target: 'entity:收入',
          type: 'produces_entity',
          meta: { entity_type: 'Metric' },
        },
      ],
      counts: { chunks: null, entities: 1, relations: 0, citations: 2 },
      truncated: { entities: false, relations: false, chunks: false },
    });
  });

  it('chunks 计数与 truncated 标记按后端值透传', () => {
    const mapped = mapLineage(
      makeLineageRow({
        counts: { chunks: 120, entities: 100, relations: 40, citations: 0 },
        truncated: { entities: true, relations: true, chunks: true },
      }),
    );

    expect(mapped.counts).toEqual({
      chunks: 120,
      entities: 100,
      relations: 40,
      citations: 0,
    });
    expect(mapped.truncated).toEqual({
      entities: true,
      relations: true,
      chunks: true,
    });
  });

  it('counts/truncated 整体缺失时与 nodes/edges 同一口径兜底', () => {
    const row = makeLineageRow();
    delete row.counts;
    delete row.truncated;

    const mapped = mapLineage(row);

    expect(mapped.counts).toEqual({
      chunks: null,
      entities: 0,
      relations: 0,
      citations: 0,
    });
    expect(mapped.truncated).toEqual({
      entities: false,
      relations: false,
      chunks: false,
    });
  });
});

describe('接口封装', () => {
  it('updateAssetMetadata 原样发送 patch，不注入 asset_type/asset_id/tenant_id', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(makeDetailRow());
    const patch = { owner: 'alice', sensitivity: null };

    await updateAssetMetadata('document', 'doc-1', patch);

    expect(apiClient.put).toHaveBeenCalledTimes(1);
    expect(apiClient.put).toHaveBeenCalledWith(
      '/data-governance/assets/document/doc-1/metadata',
      patch,
    );
    const body = vi.mocked(apiClient.put).mock.calls[0][1] as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(['owner', 'sensitivity']);
  });

  it('listAssets 走目录端点、按 buildListParams 传参并映射列表项', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [makeItemRow({ quality_verdict: 'ok' })],
      total: 7,
    });

    const result = await listAssets({
      assetType: 'document',
      keyword: ' 报告 ',
      page: 1,
      pageSize: 20,
    });

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/assets', {
      params: {
        asset_type: 'document',
        keyword: '报告',
        page: 1,
        page_size: 20,
      },
    });
    expect(result.total).toBe(7);
    expect(result.items).toHaveLength(1);
    expect(result.items[0].assetId).toBe('doc-1');
    expect(result.items[0].qualityVerdict).toBe('ok');
  });

  it('getAsset 与 getAssetLineage 的路径与参数口径正确', async () => {
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce(makeDetailRow())
      .mockResolvedValueOnce(makeLineageRow());

    const detail = await getAsset('document', 'doc-1');
    expect(apiClient.get).toHaveBeenNthCalledWith(
      1,
      '/data-governance/assets/document/doc-1',
    );
    expect(detail.mimeType).toBe('application/pdf');

    const lineage = await getAssetLineage('document', 'doc-1', 'down', 2);
    expect(apiClient.get).toHaveBeenNthCalledWith(
      2,
      '/data-governance/assets/document/doc-1/lineage',
      { params: { direction: 'down', depth: 2 } },
    );
    expect(lineage.counts.chunks).toBeNull();
  });

  it('getAssetLineage 默认方向 both、深度 1', async () => {
    vi.mocked(apiClient.get).mockResolvedValue(makeLineageRow());

    await getAssetLineage('document', 'doc-1');

    expect(apiClient.get).toHaveBeenCalledWith(
      '/data-governance/assets/document/doc-1/lineage',
      { params: { direction: 'both', depth: 1 } },
    );
  });

  it('archiveAsset/restoreAsset/destroyAsset POST 到 KB 网关并带 knowledge_base_id', async () => {
    vi.mocked(apiClient.post)
      .mockResolvedValueOnce({ id: 'doc/1', status: 'archived', archived: true })
      .mockResolvedValueOnce({ id: 'doc/1', status: 'pending', restored: true })
      .mockResolvedValueOnce({ id: 'doc/1', deleted: true });

    const archived = await archiveAsset('document', 'doc/1', 'kb-1');
    const restored = await restoreAsset('document', 'doc/1', 'kb-1');
    const destroyed = await destroyAsset('document', 'doc/1', 'kb-1');

    expect(apiClient.post).toHaveBeenNthCalledWith(
      1,
      '/knowledge-base/documents/doc%2F1/archive',
      undefined,
      { params: { knowledge_base_id: 'kb-1' } },
    );
    expect(apiClient.post).toHaveBeenNthCalledWith(
      2,
      '/knowledge-base/documents/doc%2F1/restore',
      undefined,
      { params: { knowledge_base_id: 'kb-1' } },
    );
    expect(apiClient.post).toHaveBeenNthCalledWith(
      3,
      '/knowledge-base/documents/doc%2F1/destroy',
      undefined,
      { params: { knowledge_base_id: 'kb-1' } },
    );
    // 返回后端最小状态模型，7b 可直接更新本地行。
    expect(archived).toEqual({ id: 'doc/1', status: 'archived', archived: true });
    expect(restored).toEqual({ id: 'doc/1', status: 'pending', restored: true });
    expect(destroyed).toEqual({ id: 'doc/1', deleted: true });
  });

  it('生命周期三种操作都拒绝非文档资产，且不发请求', async () => {
    await expect(archiveAsset('data_source', 'ds-1', 'kb-1')).rejects.toThrow();
    await expect(restoreAsset('data_source', 'ds-1', 'kb-1')).rejects.toThrow();
    await expect(destroyAsset('data_source', 'ds-1', 'kb-1')).rejects.toThrow();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('getRetentionPolicy 走 data-governance 端点，有 scopeId 时带上', async () => {
    vi.mocked(apiClient.get).mockResolvedValue(makeRetentionRow());

    const policy = await getRetentionPolicy('kb', 'kb-1');

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/retention-policy', {
      params: { scope_type: 'kb', scope_id: 'kb-1' },
    });
    expect(policy).toEqual({
      scope: 'kb',
      effectiveFrom: 'kb',
      retentionDays: 180,
      graceDays: 14,
      sources: { retentionDays: 'kb', graceDays: 'tenant' },
    });
  });

  it('getRetentionPolicy 租户级不带 scope_id', async () => {
    vi.mocked(apiClient.get).mockResolvedValue(makeRetentionRow());

    await getRetentionPolicy('tenant');

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/retention-policy', {
      params: { scope_type: 'tenant' },
    });
  });

  it('setRetentionPolicy PUT snake_case，只传调用方给的键（显式 null 保留）', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(makeRetentionRow());

    await setRetentionPolicy('document', 'doc-1', { retention_days: null });

    expect(apiClient.put).toHaveBeenCalledWith('/data-governance/retention-policy', {
      scope_type: 'document',
      scope_id: 'doc-1',
      retention_days: null,
    });
    const body = vi.mocked(apiClient.put).mock.calls[0][1] as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(['retention_days', 'scope_id', 'scope_type']);
  });

  it('setRetentionPolicy 两个字段都给时都发送（grace_days=0 是合法覆盖）', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(makeRetentionRow());

    await setRetentionPolicy('kb', 'kb-1', { retention_days: 30, grace_days: 0 });

    expect(apiClient.put).toHaveBeenCalledWith('/data-governance/retention-policy', {
      scope_type: 'kb',
      scope_id: 'kb-1',
      retention_days: 30,
      grace_days: 0,
    });
    const body = vi.mocked(apiClient.put).mock.calls[0][1] as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual([
      'grace_days',
      'retention_days',
      'scope_id',
      'scope_type',
    ]);
  });

  it('setRetentionPolicy 空 patch 提前抛错，不发请求', async () => {
    await expect(setRetentionPolicy('document', 'doc-1', {})).rejects.toThrow(
      '没有可更新的保留策略字段',
    );
    expect(apiClient.put).not.toHaveBeenCalled();
  });

  it('setRetentionPolicy 返回更新后的生效策略（camelCase 映射）', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(makeRetentionRow());

    const policy = await setRetentionPolicy('document', 'doc-1', { grace_days: 7 });

    expect(policy).toEqual({
      scope: 'kb',
      effectiveFrom: 'kb',
      retentionDays: 180,
      graceDays: 14,
      sources: { retentionDays: 'kb', graceDays: 'tenant' },
    });
  });
});

// ── 第 4 期分级分类 ──────────────────────────────────────────────────

describe('分级配置与默认值接口', () => {
  it('listClassificationConfig 映射 levels/categories 的 snake → camel', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      levels: [
        { code: 'public', name: '公开', rank: 1, is_active: true },
        { code: 'l4', name: '评审级', rank: 4, is_active: false },
      ],
      categories: [
        { code: 'pii', name: '个人信息', is_active: true },
        { code: 'legacy', name: '历史', is_active: false },
      ],
    });

    const config = await listClassificationConfig();

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/classification-config');
    expect(config).toEqual({
      levels: [
        { code: 'public', name: '公开', rank: 1, isActive: true },
        { code: 'l4', name: '评审级', rank: 4, isActive: false },
      ],
      categories: [
        { code: 'pii', name: '个人信息', isActive: true },
        { code: 'legacy', name: '历史', isActive: false },
      ],
    });
  });

  it('listClassificationConfig 缺字段时兜底为空列表', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({});

    await expect(listClassificationConfig()).resolves.toEqual({
      levels: [],
      categories: [],
    });
  });

  it('getClassificationDefault 走 GET，带 scope_type/scope_id 并映射', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      scope_type: 'kb',
      scope_id: 'kb-1',
      level_code: 'internal',
      categories: ['pii'],
    });

    const value = await getClassificationDefault('kb', 'kb-1');

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/classification-default', {
      params: { scope_type: 'kb', scope_id: 'kb-1' },
    });
    expect(value).toEqual({
      scopeType: 'kb',
      scopeId: 'kb-1',
      levelCode: 'internal',
      categories: ['pii'],
    });
  });

  it('getClassificationDefault 租户级不带 scope_id，响应缺字段时按入参回落', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({});

    const value = await getClassificationDefault('tenant');

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/classification-default', {
      params: { scope_type: 'tenant' },
    });
    expect(value).toEqual({
      scopeType: 'tenant',
      scopeId: '',
      levelCode: null,
      categories: null,
    });
  });

  it('kb 级缺 scope_id / 纯空白时抛错且不发请求', async () => {
    await expect(getClassificationDefault('kb', '   ')).rejects.toThrow();
    await expect(
      setClassificationDefault('kb', '', { level_code: 'internal' }),
    ).rejects.toThrow();
    expect(apiClient.get).not.toHaveBeenCalled();
    expect(apiClient.put).not.toHaveBeenCalled();
  });

  it('tenant 级纯空白 scope_id 视为未提供（不传 scope_id）', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({});
    await getClassificationDefault('tenant', '   ');
    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/classification-default', {
      params: { scope_type: 'tenant' },
    });

    vi.mocked(apiClient.put).mockResolvedValue({
      scope_type: 'tenant',
      level_code: 'internal',
      categories: null,
    });
    await setClassificationDefault('tenant', '   ', { level_code: 'internal' });
    expect(apiClient.put).toHaveBeenCalledWith('/data-governance/classification-default', {
      scope_type: 'tenant',
      level_code: 'internal',
    });
  });

  it('setClassificationDefault 只发 patch 里出现的键（显式 null 保留）', async () => {
    vi.mocked(apiClient.put).mockResolvedValue({
      scope_type: 'kb',
      scope_id: 'kb-1',
      level_code: 'internal',
      categories: null,
    });

    const value = await setClassificationDefault('kb', 'kb-1', { categories: null });

    expect(apiClient.put).toHaveBeenCalledWith('/data-governance/classification-default', {
      scope_type: 'kb',
      scope_id: 'kb-1',
      categories: null,
    });
    const body = vi.mocked(apiClient.put).mock.calls[0][1] as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(['categories', 'scope_id', 'scope_type']);
    expect(value).toEqual({
      scopeType: 'kb',
      scopeId: 'kb-1',
      levelCode: 'internal',
      categories: null,
    });
  });

  it('upsertClassificationLevel / upsertClassificationCategory 走 PUT 且路径编码', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(undefined);

    await upsertClassificationLevel('l 4', { name: '评审级', rank: 4 });
    await upsertClassificationCategory('pii/x', { is_active: false });

    expect(apiClient.put).toHaveBeenNthCalledWith(
      1,
      '/data-governance/classification-levels/l%204',
      { name: '评审级', rank: 4 },
    );
    expect(apiClient.put).toHaveBeenNthCalledWith(
      2,
      '/data-governance/classification-categories/pii%2Fx',
      { is_active: false },
    );
  });

  it('batchUpdateClassification 原样 POST payload 并返回 updated', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ updated: 3 });
    const payload = {
      assets: [{ asset_type: 'document' as const, asset_id: 'doc-1' }],
      sensitivity: 'internal',
      categories: [],
    };

    const result = await batchUpdateClassification(payload);

    expect(apiClient.post).toHaveBeenCalledWith(
      '/data-governance/assets/classification/batch',
      payload,
    );
    expect(result).toEqual({ updated: 3 });
  });
});
