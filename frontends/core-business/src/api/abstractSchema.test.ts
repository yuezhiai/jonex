import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getData, request } from './request';
import {
  getAbstractSchemaHistoryDetail,
  listAbstractSchemaHistory,
  mapAbstractSchemaHistoryDetail,
  mapAbstractSchemaHistoryList,
  mapConstructResult, mapFocusPreview, mapGenerateResult, mapImportResult, restoreAbstractSchemaHistory,
  saveAbstractSchema,
} from './abstractSchema.js';

vi.mock('./request', () => ({
  request: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  getData: vi.fn(),
}));

const getDataMock = vi.mocked(getData);
const getMock = vi.mocked(request.get);
const postMock = vi.mocked(request.post);
const putMock = vi.mocked(request.put);

describe('保存历史（list / detail / restore）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('list：GET 路径 + kb/limit 参数，映射 items/total', async () => {
    getDataMock.mockResolvedValueOnce({
      items: [{
        id: 's1', schema_version: 6, status: 'active', saved_by: 'alice',
        source: 'restore', created_at: '2026-10-27T10:00:00Z',
      }],
      total: 3,
    });

    const out = await listAbstractSchemaHistory('kb_1', 5);

    expect(getMock).toHaveBeenCalledWith(
      '/knowledge-base/ontology/abstract-schema/history',
      { params: { knowledge_base_id: 'kb_1', limit: 5 } },
    );
    expect(out).toEqual({
      items: [{
        id: 's1', schemaVersion: 6, status: 'active', savedBy: 'alice',
        source: 'restore', createdAt: '2026-10-27T10:00:00Z',
      }],
      total: 3,
    });
  });

  it('list：不传 limit 时 params 不带 limit；缺键/脏来源安全回落', async () => {
    getDataMock.mockResolvedValueOnce({
      items: [{ id: 's1', schema_version: 1, status: 'wat', source: 'wat' }],
    });

    const out = await listAbstractSchemaHistory('kb_1');

    expect(getMock.mock.calls[0]![1]).toEqual({ params: { knowledge_base_id: 'kb_1' } });
    expect(out.items[0]).toMatchObject({
      status: 'proposed', source: 'manual', savedBy: null, createdAt: null,
    });
    expect(out.total).toBe(1); // 无 total 时回落到本页条数
  });

  it('detail：GET 路径含 snapshot_id + kb 参数，schema 内容被映射', async () => {
    getDataMock.mockResolvedValueOnce({
      id: 's1', schema_version: 6, status: 'proposed', saved_by: 'alice',
      source: 'manual', created_at: null, knowledge_base_id: 'kb_1',
      schema: {
        entities: [{ name: 'Job', sources: [], attributes: [] }],
        relations: [],
        metrics: [{ name: 'm1' }],
      },
    });

    const out = await getAbstractSchemaHistoryDetail('s1', 'kb_1');

    expect(getMock).toHaveBeenCalledWith(
      '/knowledge-base/ontology/abstract-schema/history/s1',
      { params: { knowledge_base_id: 'kb_1' } },
    );
    expect(out.knowledgeBaseId).toBe('kb_1');
    expect(out.schema.entities[0]!.name).toBe('Job');
    expect(out.schema.metrics?.[0]!.name).toBe('m1');
    expect(out.source).toBe('manual');
  });

  it('restore：POST 路径 + body（kb + 当前版本作为乐观锁）', async () => {
    getDataMock.mockResolvedValueOnce({
      id: 'row_1', knowledge_base_id: 'kb_1', schema_version: 8, status: 'active',
      schema: { entities: [], relations: [], metrics: [] }, stale_count: 0,
    });

    const out = await restoreAbstractSchemaHistory('s1', 'kb_1', 7);

    expect(postMock).toHaveBeenCalledWith(
      '/knowledge-base/ontology/abstract-schema/history/s1/restore',
      { knowledge_base_id: 'kb_1', expected_schema_version: 7 },
    );
    expect(out.schemaVersion).toBe(8);
    expect(out.status).toBe('active');
  });

  it('mapper：畸形回包给空兜底，不抛', () => {
    expect(mapAbstractSchemaHistoryList(undefined)).toEqual({ items: [], total: 0 });
    expect(mapAbstractSchemaHistoryList({ items: 'x' }).items).toEqual([]);
    expect(mapAbstractSchemaHistoryDetail(undefined)).toMatchObject({
      id: '', knowledgeBaseId: '', schema: { entities: [], relations: [], metrics: [] },
    });
  });
});

describe('mapConstructResult', () => {
  it('映射后端摘要为前端形状（缺键安全回落）', () => {
    const mapped = mapConstructResult({
      schema_version: 45,
      attributes: { added: 3120 },
      relations: { added: 121 },
      suggestions: { added: 3 },
      names: { enabled: true, patched: 2800, remaining: 320 },
      warnings: ['补边探针失败 x'],
    });
    expect(mapped).toEqual({
      schemaVersion: 45,
      attributesAdded: 3120,
      relationsAdded: 121,
      suggestionsAdded: 3,
      namesEnabled: true,
      namesPatched: 2800,
      namesRemaining: 320,
      landings: { entities: 0, attributes: 0, error: null },
      warnings: ['补边探针失败 x'],
    });
  });

  it('自动落点统计原样映射', () => {
    const mapped = mapConstructResult({
      landings: { entities: 2, attributes: 4, error: 'llm down' },
    });
    expect(mapped.landings).toEqual({
      entities: 2, attributes: 4, error: 'llm down',
    });
  });

  it('空/畸形回包给全零兜底，不抛', () => {
    expect(mapConstructResult(undefined)).toEqual({
      schemaVersion: 0,
      attributesAdded: 0,
      relationsAdded: 0,
      suggestionsAdded: 0,
      namesEnabled: false,
      namesPatched: 0,
      namesRemaining: 0,
      landings: { entities: 0, attributes: 0, error: null },
      warnings: [],
    });
    expect(mapConstructResult({ warnings: 'not-a-list' }).warnings).toEqual([]);
  });
});

describe('mapFocusPreview', () => {
  it('映射种子、邻居与 warnings（含 role 归一）', () => {
    const mapped = mapFocusPreview({
      seeds: [
        { data_source_id: 'ds_a', entity_name: 'Job', source_name: '招聘库',
          description: '职位', role: 'focus', concept: '职位' },
        { data_source_id: 'ds_b', entity_name: 'Jobs', source_name: '订单库',
          description: '岗位', role: 'partner', concept: '职位' },
        { data_source_id: 'ds_c', entity_name: 'X', role: 'wat' },
      ],
      neighbors: [
        { data_source_id: 'ds_a', entity_name: 'Application', source_name: '招聘库',
          via: [{ seed_data_source_id: 'ds_a', seed_entity_name: 'Job',
                  relation_name: 'REF_APP' }] },
      ],
      warnings: ['解析出 120 个重点实体'],
    });
    expect(mapped.seeds[0]).toEqual({
      dataSourceId: 'ds_a', entityName: 'Job', sourceName: '招聘库',
      description: '职位', role: 'focus', concept: '职位',
    });
    expect(mapped.seeds[2].role).toBe('focus'); // 脏 role 归一到 focus
    expect(mapped.neighbors[0].via[0].relationName).toBe('REF_APP');
    expect(mapped.warnings).toEqual(['解析出 120 个重点实体']);
  });

  it('空/畸形回包给空兜底，不抛', () => {
    expect(mapFocusPreview(undefined)).toEqual({ seeds: [], neighbors: [], warnings: [] });
    expect(mapFocusPreview({ warnings: 'not-a-list' }).warnings).toEqual([]);
    expect(mapFocusPreview({ seeds: 'x' }).seeds).toEqual([]);
  });
});

describe('mapGenerateResult', () => {
  it('带回 focus 摘要（snake → camel）与 droppedSources', () => {
    const mapped = mapGenerateResult({
      status: 'proposed',
      schema: { entities: [], relations: [] },
      dropped_sources: 2,
      focus: { seeds: 3, partners: 1, neighbors: 5, included_tables: 9, excluded_tables: 30,
        dropped_seeds: 4 },
    });
    expect(mapped.droppedSources).toBe(2);
    expect(mapped.focus).toEqual({
      seeds: 3, partners: 1, neighbors: 5, includedTables: 9, excludedTables: 30,
      droppedSeeds: 4,
    });
  });

  it('focus.dropped_seeds 缺失 → 回 0', () => {
    const mapped = mapGenerateResult({
      schema: { entities: [], relations: [] },
      focus: { seeds: 3, partners: 1, neighbors: 5, included_tables: 9, excluded_tables: 30 },
    });
    expect(mapped.focus?.droppedSeeds).toBe(0);
  });

  it('无 focus 键（全量路径）→ undefined', () => {
    expect(mapGenerateResult({}).focus).toBeUndefined();
    expect(mapGenerateResult({}).droppedSources).toBe(0);
  });
});

describe('mapImportResult', () => {
  it('dry_run 摘要：snake → camel、缺键回落、schema 置 null', () => {
    const mapped = mapImportResult({
      dry_run: true,
      summary: { entities_added: 2, entities_updated: 1, attributes_added: 3,
                 ignored_sections: ['constraints(1)', 'disambiguation'] },
      warnings: ['关系 R 的端点不在实体集里（含已有实体），已跳过'],
    });
    expect(mapped).toEqual({
      dryRun: true,
      summary: { entitiesAdded: 2, entitiesUpdated: 1, attributesAdded: 3,
                 ignoredSections: ['constraints(1)', 'disambiguation'] },
      warnings: ['关系 R 的端点不在实体集里（含已有实体），已跳过'],
      schema: null,
    });
  });

  it('正式导入：平铺的 save 回包映射成 schema（id 判定同 mapSchema）', () => {
    const mapped = mapImportResult({
      dry_run: false, summary: {}, warnings: [],
      id: 'row_1', knowledge_base_id: 'kb_1', schema_version: 8, status: 'active',
      schema: { entities: [], relations: [] }, stale_count: 0,
    });
    expect(mapped.dryRun).toBe(false);
    expect(mapped.schema?.schemaVersion).toBe(8);
    expect(mapped.summary.entitiesAdded).toBe(0);
  });
});

describe('落点语义槽 P1-5（filter/aggregation/grain/enabled）', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('读映射：四键原样带回；缺键不物化空键', () => {
    const mapped = mapImportResult({
      dry_run: false, summary: {}, warnings: [],
      id: 'row_1', knowledge_base_id: 'kb_1', schema_version: 1, status: 'proposed',
      schema: {
        entities: [{
          name: 'E', description: '', aliases: [], sources: [],
          attributes: [{
            name: 'a', type: 'string', primary_key: false, required: false,
            sources: [
              { data_source_id: 'ds', entity_name: 'T', attribute_name: 'c',
                filter: 'c > 0', aggregation: 'SUM', grain: 'per day', enabled: false },
              { data_source_id: 'ds', entity_name: 'T', attribute_name: 'd' },
            ],
          }],
        }],
        relations: [],
      },
      stale_count: 0,
    });
    const [withSlots, bare] = mapped.schema!.schema.entities[0]!.attributes[0]!.sources;
    expect(withSlots).toMatchObject({
      filter: 'c > 0', aggregation: 'SUM', grain: 'per day', enabled: false,
    });
    expect('filter' in bare!).toBe(false);
    expect('aggregation' in bare!).toBe(false);
    expect('grain' in bare!).toBe(false);
    expect('enabled' in bare!).toBe(false);
  });

  it('保存序列化：四键只在有值时写出（enabled 仅布尔）', async () => {
    getDataMock.mockResolvedValueOnce({
      id: 'row_1', knowledge_base_id: 'kb_1', schema_version: 2, status: 'proposed',
      schema: { entities: [], relations: [] }, stale_count: 0,
    });
    await saveAbstractSchema({
      knowledgeBaseId: 'kb_1', status: 'proposed', expectedSchemaVersion: 1,
      schema: {
        entities: [{
          name: 'E', description: '', aliases: [], sources: [],
          attributes: [{
            name: 'a', type: 'string', primaryKey: false, required: false,
            sources: [
              { dataSourceId: 'ds', entityName: 'T', attributeName: 'c', stale: false,
                filter: 'c > 0', aggregation: 'SUM', grain: 'per day', enabled: false },
              { dataSourceId: 'ds', entityName: 'T', attributeName: 'd', stale: false,
                enabled: true },
              { dataSourceId: 'ds', entityName: 'T', attributeName: 'e', stale: false },
            ],
          }],
        }],
        relations: [],
      },
    });
    const sent = putMock.mock.calls[0]![1] as any;
    const [withSlots, enabledOnly, bare] = sent.schema.entities[0].attributes[0].sources;
    expect(withSlots).toMatchObject({
      data_source_id: 'ds', attribute_name: 'c',
      filter: 'c > 0', aggregation: 'SUM', grain: 'per day', enabled: false,
    });
    expect(enabledOnly.enabled).toBe(true);
    expect('filter' in enabledOnly).toBe(false);
    expect('filter' in bare).toBe(false);
    expect('aggregation' in bare).toBe(false);
    expect('grain' in bare).toBe(false);
    expect('enabled' in bare).toBe(false);
  });
});
