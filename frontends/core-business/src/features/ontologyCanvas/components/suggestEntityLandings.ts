/**
 * 实体级落点建议（纯函数，无 React / 无 IO）。
 *
 * 候选面 = 调用方给的**全部数据源 × 全部源实体**（跨源一键建议），排除该抽象实体
 * **已登记**的 (源, 源实体) 对（不看 `included`——已登记就不该再建议，与一期 picker
 * 的 disabled 判据同口径）。匹配判据复用 `matchKernel`，与字段级同一套。
 *
 * 设计见 docs/superpowers/specs/2026-10-16-abstract-landing-suggest-deepening-design.md §7。
 */
import {
  EMPTY_SYNONYM_INDEX, HIT_ORDER, hitKind,
  type HitKind, type MatchTerms, type SynonymIndex,
} from './matchKernel';
import type { EntityType } from '../domain/index.js';

/** 一个候选源实体（编译 schema 的实体 + 它属于哪个源）。 */
export interface CandidateEntity {
  dataSourceId: string;
  /** 源显示名（用于分组头）；缺省时 UI 回落 `dataSourceId` */
  sourceName?: string;
  name: string;
  displayName?: string;
  aliases?: string[];
}

/** 一条候选落点（`hit` 只用于展示，不写进落点）。 */
export interface EntitySuggestion {
  dataSourceId: string;
  entityName: string;
  hit: HitKind;
}

/** 一个数据源下的候选：`strong` 默认展开，`weak`（token 档）默认折叠。 */
export interface EntitySuggestionGroup {
  dataSourceId: string;
  sourceName?: string;
  strong: EntitySuggestion[];
  weak: EntitySuggestion[];
}

const pairKey = (dataSourceId: string, entityName: string): string =>
  `${dataSourceId}\u0000${entityName}`;

/** 实体级候选：排除已登记项 → 同 (源, 实体) 取最高档 → 强弱分档 → 按源分组。 */
export function suggestEntityLandings(
  entity: Pick<EntityType, 'name' | 'displayName' | 'aliases' | 'landings'>,
  candidates: CandidateEntity[],
  synonymIndex: SynonymIndex = EMPTY_SYNONYM_INDEX,
): EntitySuggestionGroup[] {
  const taken = new Set((entity.landings ?? []).map((l) => pairKey(l.dataSourceId, l.entityName)));
  const target: MatchTerms = {
    name: entity.name,
    ...(entity.displayName ? { displayName: entity.displayName } : {}),
    ...(entity.aliases?.length ? { aliases: entity.aliases } : {}),
  };

  // ① 逐候选定档；同 (源, 实体) 取最高档
  const best = new Map<string, EntitySuggestion>();
  const sourceNames = new Map<string, string>();
  for (const c of candidates) {
    if (c.sourceName) sourceNames.set(c.dataSourceId, c.sourceName);
    const key = pairKey(c.dataSourceId, c.name);
    if (taken.has(key)) continue;
    const hit = hitKind(c, target, synonymIndex);
    if (hit === null) continue;
    const current = best.get(key);
    if (current === undefined || HIT_ORDER[hit] < HIT_ORDER[current.hit]) {
      best.set(key, { dataSourceId: c.dataSourceId, entityName: c.name, hit });
    }
  }

  // ② 强 / 弱分档（token 是弱档）
  const strong = new Map<string, EntitySuggestion[]>();
  const weak = new Map<string, EntitySuggestion[]>();
  for (const item of best.values()) {
    const bucket = item.hit === 'token' ? weak : strong;
    const list = bucket.get(item.dataSourceId);
    if (list) list.push(item);
    else bucket.set(item.dataSourceId, [item]);
  }

  const byHitThenName = (a: EntitySuggestion, b: EntitySuggestion): number =>
    HIT_ORDER[a.hit] - HIT_ORDER[b.hit] || a.entityName.localeCompare(b.entityName);

  return [...new Set([...strong.keys(), ...weak.keys()])].sort().map((dataSourceId) => {
    const sourceName = sourceNames.get(dataSourceId);
    return {
      dataSourceId,
      ...(sourceName ? { sourceName } : {}),
      strong: (strong.get(dataSourceId) ?? []).sort(byHitThenName),
      weak: (weak.get(dataSourceId) ?? []).sort(byHitThenName),
    };
  });
}
