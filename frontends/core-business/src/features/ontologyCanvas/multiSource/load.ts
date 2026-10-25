/**
 * 多源视图取数：ds 列表 → 逐源拉 ds 级编译 schema →（并行）抽象 schema。
 * 读口全是现成的（data-sources / compiled-schema / abstract-schema），
 * 本文件不新增后端契约。
 */
import { request, getData, type ApiResponse } from '@/api/request';
import { listDataSources } from '@/api/dataSource';
import { getAbstractSchema } from '@/api/abstractSchema';
import { compiledSchemaSources } from '@/utils/dataSourceDisplay';

import type { ServerSchema } from '../adapters.js';
import type { ConceptInput, SourceInput } from './model.js';

export interface MultiSourceData {
  sources: SourceInput[];
  concepts: ConceptInput[];
  /** 抽象 schema 的状态（proposed = 草稿，未参与检索）；没有抽象 schema 时为 null */
  abstractStatus: 'proposed' | 'active' | null;
}

/** 导出给 F3 的 LandingSection 复用（「从源添加实体」要列某源的实体清单）。 */
export async function getCompiledSchema(kbId: string, dataSourceId: string): Promise<ServerSchema | null> {
  // 与 OntologyCanvasPage 同款调用：信封由 getData 解两层，服务端「没有这一份」回 data:null
  return getData<ServerSchema | null>(
    request.get<ApiResponse<ServerSchema | null>>('/knowledge-base/ontology/compiled-schema', {
      params: { knowledge_base_id: kbId, data_source_id: dataSourceId },
    }),
  );
}

export async function loadMultiSourceData(kbId: string): Promise<MultiSourceData> {
  const [dsList, abstract] = await Promise.all([listDataSources(kbId), getAbstractSchema(kbId)]);

  // ETL 来源没有自己的编译 schema（config 指向一个 database 源）→ 不进多源视图，
  // 否则会多出一条永远没有 schema 的幻影源列。
  const sources = await Promise.all(
    compiledSchemaSources(dsList).map(async (ds) => ({
      id: ds.id,
      name: ds.name,
      schema: await getCompiledSchema(kbId, ds.id),
    })),
  );

  // v2：实体落点取代概念（只把 included 落点画进叠层——不纳入的不是 schema 的一部分；
  // stale 仍透传，view 层用它做「只看跨源」的过滤与提示）
  const concepts: ConceptInput[] = (abstract?.schema.entities ?? []).map((entity) => ({
    name: entity.name,
    landings: entity.sources
      .filter((source) => source.included !== false)
      .map((source) => ({
        sourceId: source.dataSourceId,
        entityName: source.entityName,
        stale: source.stale,
      })),
  }));

  return { sources, concepts, abstractStatus: abstract?.status ?? null };
}
