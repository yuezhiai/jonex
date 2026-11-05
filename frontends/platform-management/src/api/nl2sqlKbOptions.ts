/**
 * 演化台「发起 run」弹窗的下拉选项：知识库 / 数据源。
 *
 * 「可优化」= 结构可跑判据（与 CLI 硬前置同源，tests/eval/nl2sql/evolve.py `_get_row`）：
 * - alias：该 KB 有抽象 schema 行（`schema.entities` 非空）
 * - query_rule：该 KB 下至少一个 database 源有编译 schema 行（`entity_types` 非空）
 *
 * 只读现成读口，直连 /knowledge-base/*（不经演化桥接；先例见 dataSourceConnection.ts）。
 * 单项查询失败 = 该项视为不可优化，不炸整体；但整族探测全挂（半死）升级为整体失败（T1-1）。
 */
import apiClient from './request';

export interface KbOption {
  id: string;
  name: string;
}

export interface DsOption {
  id: string;
  name: string;
}

export interface QueryRuleKb {
  kb: KbOption;
  /** 该 KB 下有编译 schema 的 database 源 */
  sources: DsOption[];
}

export interface OptimizableOptions {
  aliasKbs: KbOption[];
  queryRuleKbs: QueryRuleKb[];
}

export const EMPTY_OPTIONS: OptimizableOptions = { aliasKbs: [], queryRuleKbs: [] };

/** 抽象 schema 行「存在」的判据（data.schema.entities 非空）。 */
export function hasAbstractSchema(data: unknown): boolean {
  const entities = (data as { schema?: { entities?: unknown[] } } | null)?.schema?.entities;
  return Array.isArray(entities) && entities.length > 0;
}

/** 编译 schema 行「存在」的判据（data.entity_types 非空）。 */
export function hasCompiledSchema(data: unknown): boolean {
  const entityTypes = (data as { entity_types?: unknown[] } | null)?.entity_types;
  return Array.isArray(entityTypes) && entityTypes.length > 0;
}

interface RawKbItem {
  id: string;
  name?: string;
}

interface RawDsItem {
  id: string;
  name?: string;
  access_type?: string;
}

/** 探测结果：ok=false 表示该查询「抛了」（端点不可达），与「合法但没有」区分开。 */
interface Probe<T> {
  ok: boolean;
  value: T;
}

async function fetchAliasFlag(kbId: string): Promise<Probe<boolean>> {
  try {
    const data = await apiClient.get<unknown>('/knowledge-base/ontology/abstract-schema', {
      params: { knowledge_base_id: kbId },
    });
    return { ok: true, value: hasAbstractSchema(data) };
  } catch {
    return { ok: false, value: false };
  }
}

async function fetchRunnableSources(kbId: string): Promise<Probe<DsOption[]>> {
  let list: { items?: RawDsItem[] } | RawDsItem[];
  try {
    list = await apiClient.get<{ items?: RawDsItem[] } | RawDsItem[]>(
      '/knowledge-base/data-sources',
      { params: { knowledge_base_id: kbId } },
    );
  } catch {
    return { ok: false, value: [] };
  }
  const items = Array.isArray(list) ? list : list?.items ?? [];
  // 先滤 database：对 file/api 源查 compiled-schema 只会白吃 400
  const dbSources = items.filter((d) => d.access_type === 'database');
  const flags = await Promise.all(
    dbSources.map(async (d): Promise<DsOption | null> => {
      try {
        const data = await apiClient.get<unknown>('/knowledge-base/ontology/compiled-schema', {
          params: { knowledge_base_id: kbId, data_source_id: d.id },
        });
        return hasCompiledSchema(data) ? { id: d.id, name: d.name || d.id } : null;
      } catch {
        return null;
      }
    }),
  );
  return { ok: true, value: flags.filter((x): x is DsOption => x !== null) };
}

/** 拉取两组下拉选项。单项探测失败容忍（失败项视为不可优化）；整族探测全挂才整体失败。 */
/** 该 KB 可评测的数据源（database 且有编译 schema）；失败返回空列表。 */
export async function listRunnableSources(kbId: string): Promise<DsOption[]> {
  const res = await fetchRunnableSources(kbId);
  return res.value;
}

export async function fetchOptimizableOptions(): Promise<OptimizableOptions> {
  const kbList = await apiClient.get<{ items?: RawKbItem[] } | RawKbItem[]>(
    '/knowledge-base/knowledge-info',
    { params: { limit: 100 } },
  );
  const items = Array.isArray(kbList) ? kbList : kbList?.items ?? [];
  const results = await Promise.all(
    items.map(async (raw): Promise<{
      kb: KbOption; alias: boolean; aliasOk: boolean; sources: DsOption[]; dsOk: boolean;
    }> => {
      const kb: KbOption = { id: raw.id, name: raw.name || raw.id };
      const [alias, sources] = await Promise.all([
        fetchAliasFlag(kb.id),
        fetchRunnableSources(kb.id),
      ]);
      return { kb, alias: alias.value, aliasOk: alias.ok, sources: sources.value, dsOk: sources.ok };
    }),
  );
  // T1-1：knowledge-info 活着、但整族探测端点全挂时，空池与「真的没有可优化项」不可区分——
  // 全族探针都抛才升级为整体失败（UI 走 optionsError + 重试）。个别失败照旧静默容忍。
  // 粗粒度已知限制：alias 一族全挂会连带 query_rule 一脸报错（反之亦然）；两族是同一
  // knowledge-base 服务的不同路由，「独立半死」只可能是路由级故障，暂不为它加 per-kind 态。
  if (items.length > 0) {
    const aliasAllDown = results.every((r) => !r.aliasOk);
    const dsAllDown = results.every((r) => !r.dsOk);
    if (aliasAllDown || dsAllDown) {
      throw new Error('下拉选项探测整体失败：knowledge-base 探测端点一族不可达');
    }
  }
  return {
    aliasKbs: results.filter((r) => r.alias).map((r) => r.kb),
    queryRuleKbs: results
      .filter((r) => r.sources.length > 0)
      .map((r) => ({ kb: r.kb, sources: r.sources })),
  };
}
