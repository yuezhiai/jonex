/**
 * 血缘影响分析与覆盖率 API（W2.4a）。
 *
 * 后端：
 * - `GET /knowledge-base/ontology/lineage-impact`
 * - `GET /knowledge-base/ontology/lineage-coverage`
 *
 * 两者都是只读；错误不在这里吞，页面原样透出后端 message。
 */
import { request, getData } from './request';

const IMPACT_BASE = '/knowledge-base/ontology/lineage-impact';
const COVERAGE_BASE = '/knowledge-base/ontology/lineage-coverage';

export type LineageDirection = 'upstream' | 'downstream' | 'both';

export interface LineageImpactNode {
  table: string;
  column?: string | null;
}

export interface LineageImpactEdge {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  origin: string;
  confidence: number;
  /** 是否逆数据流遍历（upstream / both 时可能为 true）；端点始终是数据流端点。 */
  reversed: boolean;
}

export interface LineageImpactLevel {
  hop: number;
  nodes: LineageImpactNode[];
  edges: LineageImpactEdge[];
}

export interface LineageImpactResult {
  anchor: LineageImpactNode;
  direction: LineageDirection;
  hops: number;
  levels: LineageImpactLevel[];
  affected: { tables: string[]; columns: string[] };
  total_edges: number;
  truncated: boolean;
  /** 读边失败 —— 结果不可用，不等于「没有边」。 */
  unknown: boolean;
}

export interface LineageImpactQuery {
  table: string;
  column?: string;
  direction?: LineageDirection;
  hops?: number;
  min_origin?: string;
}

export async function getLineageImpact(
  kbId: string,
  dsId: string,
  query: LineageImpactQuery,
): Promise<LineageImpactResult> {
  const res = await getData<LineageImpactResult>(
    request.get(IMPACT_BASE, {
      params: {
        knowledge_base_id: kbId,
        data_source_id: dsId,
        table: query.table,
        column: query.column || undefined,
        direction: query.direction,
        hops: query.hops,
        min_origin: query.min_origin || undefined,
      },
    }),
  );
  // 读边失败/响应缺失必须显式报错，不能伪装成「没有边」的空图。
  if (!res || !Array.isArray(res.levels) || typeof res.unknown !== 'boolean') {
    throw new Error('血缘影响分析响应格式不正确');
  }
  return {
    anchor: res?.anchor ?? { table: query.table, column: query.column ?? null },
    direction: res?.direction ?? query.direction ?? 'downstream',
    hops: res?.hops ?? query.hops ?? 3,
    levels: res?.levels ?? [],
    affected: res?.affected ?? { tables: [], columns: [] },
    total_edges: res?.total_edges ?? 0,
    truncated: res?.truncated === true,
    unknown: res?.unknown === true,
  };
}

export interface LineageCoverageBucket {
  total: number;
  covered: number;
  /** 分母为 0 或读边失败时为 null —— 不是 0%。 */
  ratio: number | null;
}

export interface LineageCoverageTable {
  table: string;
  entity_name: string;
  columns_total: number;
  columns_covered: number;
  covered: boolean;
}

export interface LineageCoverage {
  data_source_id?: string;
  tables: LineageCoverageBucket;
  columns: LineageCoverageBucket;
  by_origin: Record<string, number>;
  orphan_edges: number;
  total_edges: number;
  per_table: LineageCoverageTable[];
  truncated: boolean;
  unknown: boolean;
}

export async function getLineageCoverage(
  kbId: string,
  dsId: string,
): Promise<LineageCoverage> {
  const res = await getData<LineageCoverage>(
    request.get(COVERAGE_BASE, {
      params: { knowledge_base_id: kbId, data_source_id: dsId },
    }),
  );
  // 同上：缺失的覆盖率响应不能被读成 0%。
  if (!res || !res.tables || !res.columns || typeof res.unknown !== 'boolean') {
    throw new Error('血缘覆盖率响应格式不正确');
  }
  return {
    data_source_id: res?.data_source_id,
    tables: res?.tables ?? { total: 0, covered: 0, ratio: null },
    columns: res?.columns ?? { total: 0, covered: 0, ratio: null },
    by_origin: res?.by_origin ?? {},
    orphan_edges: res?.orphan_edges ?? 0,
    total_edges: res?.total_edges ?? 0,
    per_table: res?.per_table ?? [],
    truncated: res?.truncated === true,
    unknown: res?.unknown === true,
  };
}
