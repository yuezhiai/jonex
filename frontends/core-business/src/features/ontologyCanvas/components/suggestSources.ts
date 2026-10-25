/**
 * 字段级落点自动建议（纯函数，无 React / 无 IO）。
 *
 * 匹配判据的**唯一实现**在 `matchKernel`（与实体级建议 `suggestEntityLandings` 共用）：
 * `exact > display > synonym > token`。本文件只负责**组装候选面**——在调用方给的 pairs
 * （该抽象实体已 included 的落点）内逐列判定、排除已落点、同三元组取最高档、排序。
 *
 * 边界：只在调用方给的 pairs 内出候选——一期候选约束（字段落点依附实体落点）的延续。
 */
import {
  EMPTY_SYNONYM_INDEX, HIT_ORDER, hitKind,
  type HitKind, type MatchTerms, type SynonymIndex,
} from './matchKernel';
import type { Attribute, AttributeSource } from '../domain/index.js';

export type { HitKind, SynonymIndex };

/** 编译 schema 的一列（只取匹配所需的两字段）。 */
export interface CompiledColumn {
  name: string;
  displayName?: string;
}

/** 一个已 included 落点对 + 该源实体的列清单。 */
export interface LandingPair {
  dataSourceId: string;
  entityName: string;
  columns: CompiledColumn[];
}

/** 一条候选落点 = 落点三元组 + 命中档（`hit` 只用于展示，不写进落点）。 */
export interface SourceSuggestion extends AttributeSource {
  hit: HitKind;
  /** 编译列的显示名（中文名）；仅用于展示，不写进落点。 */
  displayName?: string;
}

const keyOf = (dataSourceId: string, entityName: string, attributeName: string): string =>
  `${dataSourceId}\u0000${entityName}\u0000${attributeName}`;

/**
 * 建议候选：排除已在 `attribute.sources` 的三元组，同三元组取最高档，
 * 按 档位 → 数据源 → 源实体 → 列名 排序。
 *
 * `synonymIndex` 缺省 = 空索引 = 不接同义词表。⚠️ **这不等于「二期三档」**：`synonym`
 * 档的条件 ② 不需要同义词表，字段级的「一方主名 ↔ 另一方显示名」槽位交叉照样命中。
 */
export function suggestSources(
  attribute: Attribute,
  pairs: LandingPair[],
  synonymIndex: SynonymIndex = EMPTY_SYNONYM_INDEX,
): SourceSuggestion[] {
  const existing = new Set(
    (attribute.sources ?? []).map((s) => keyOf(s.dataSourceId, s.entityName, s.attributeName)),
  );
  const best = new Map<string, SourceSuggestion>();
  const target: MatchTerms = {
    name: attribute.name,
    ...(attribute.displayName ? { displayName: attribute.displayName } : {}),
  };

  for (const pair of pairs) {
    for (const column of pair.columns) {
      const key = keyOf(pair.dataSourceId, pair.entityName, column.name);
      if (existing.has(key)) continue;
      const hit = hitKind(column, target, synonymIndex);
      if (hit === null) continue;
      const current = best.get(key);
      if (current === undefined || HIT_ORDER[hit] < HIT_ORDER[current.hit]) {
        best.set(key, {
          dataSourceId: pair.dataSourceId,
          entityName: pair.entityName,
          attributeName: column.name,
          hit,
          ...(column.displayName ? { displayName: column.displayName } : {}),
        });
      }
    }
  }

  return [...best.values()].sort((a, b) =>
    HIT_ORDER[a.hit] - HIT_ORDER[b.hit]
    || a.dataSourceId.localeCompare(b.dataSourceId)
    || a.entityName.localeCompare(b.entityName)
    || a.attributeName.localeCompare(b.attributeName));
}
