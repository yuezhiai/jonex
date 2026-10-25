/**
 * 落点建议的匹配 kernel（纯函数，无 React、无 IO）。
 *
 * 字段级（`suggestSources`）与实体级（`suggestEntityLandings`）共用**同一套**判据，
 * 保证口径只有一份实现。设计见
 * docs/superpowers/specs/2026-10-16-abstract-landing-suggest-deepening-design.md §5。
 */

/** 命中档：exact（主名全等）> display（显示名全等）> synonym（声明式等价）> token（主名子串）。 */
export type HitKind = 'exact' | 'display' | 'synonym' | 'token';

/** 档位序（小 = 高）。排序与「取最高档」都用它。 */
export const HIT_ORDER: Record<HitKind, number> = { exact: 0, display: 1, synonym: 2, token: 3 };

/** 一侧的匹配词信息来源（字段级只有 name/displayName；实体级还带 aliases）。 */
export interface MatchTerms {
  name: string;
  displayName?: string;
  aliases?: string[];
}

/** 同义词索引：归一化 term → 组 id。组间唯一由后端保证；脏数据取先出现的组。 */
export interface SynonymIndex {
  readonly groupOf: ReadonlyMap<string, string>;
}

/** 空索引（不接同义词表时用）。 */
export const EMPTY_SYNONYM_INDEX: SynonymIndex = { groupOf: new Map() };

/** token 档的最短长度：短于此不做子串匹配，避免 `id` / `no` 这类噪声。 */
const MIN_TOKEN_LEN = 3;

/** 全角 → 半角（与后端 `ontology_synonym_service._to_halfwidth` 同口径）。 */
function toHalfwidth(value: string): string {
  let out = '';
  for (const ch of value) {
    const code = ch.codePointAt(0);
    if (code === undefined) continue;
    if (code === 0x3000) out += ' ';
    else if (code >= 0xff01 && code <= 0xff5e) out += String.fromCharCode(code - 0xfee0);
    else out += ch;
  }
  return out;
}

/**
 * 归一：全角 → 半角，再 lower，再去 `_` / 空格 / `-`。
 *
 * ⚠️ 相对二期字段级的 `normalizeTerm` **多了全角转半角**——纯 ASCII 输入行为不变，
 * 含全角输入是有意变更（修复），见 spec §5 / §11。
 */
export function normalizeTerm(value: string): string {
  return toHalfwidth(value).toLowerCase().replace(/[_\s-]/g, '');
}

/** 两侧的词集：`name` + `displayName` + `aliases` 归一后去空去重。 */
export function termsOf(x: MatchTerms): string[] {
  const raw = [x.name, x.displayName ?? '', ...(x.aliases ?? [])];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of raw) {
    const key = normalizeTerm(String(item ?? ''));
    if (key === '' || seen.has(key)) continue;
    seen.add(key);
    out.push(key);
  }
  return out;
}

/** 由同义词组构建索引；空 / 缺省 → 空索引（等价于不接同义词表）。 */
export function buildSynonymIndex(groups: Array<{ terms: string[] }> | undefined | null): SynonymIndex {
  const groupOf = new Map<string, string>();
  (groups ?? []).forEach((group, i) => {
    const groupId = String(i);
    for (const raw of group?.terms ?? []) {
      const key = normalizeTerm(String(raw ?? ''));
      if (key === '') continue;
      // 后端保证同词不在两组；这里只做「脏数据不炸」的兜底——先出现的组胜
      if (!groupOf.has(key)) groupOf.set(key, groupId);
    }
  });
  return { groupOf };
}

/**
 * 通用命中档；不命中返回 null。
 *
 * 判据（从高到低，返回首个命中）：
 *  - exact   主名归一全等
 *  - display 显示名归一全等（两侧都用 `displayName ?? name` 回退）
 *  - synonym ① 任一对词属同一同义词组；② 词集有交集
 *            （② 覆盖 exact/display 够不到的**槽位交叉**：任一侧的 `aliases` ↔ 对侧主名/显示名，
 *              以及一方主名 ↔ 另一方显示名——`display` 档比的是同一个「最可读名」槽位，比不到交叉。
 *              注意 ② 不依赖同义词表：空索引下仍可命中，别当死代码删）
 *  - token   主名互为子串，且较短串长度 ≥ 3
 */
export function hitKind(
  a: MatchTerms,
  b: MatchTerms,
  idx: SynonymIndex = EMPTY_SYNONYM_INDEX,
): HitKind | null {
  const aName = normalizeTerm(a.name);
  const bName = normalizeTerm(b.name);
  if (aName !== '' && aName === bName) return 'exact';

  const aDisplay = normalizeTerm(a.displayName ?? a.name);
  const bDisplay = normalizeTerm(b.displayName ?? b.name);
  if (aDisplay !== '' && aDisplay === bDisplay) return 'display';

  const termsA = termsOf(a);
  const termsB = termsOf(b);
  // ① 同一同义词组
  for (const x of termsA) {
    const g = idx.groupOf.get(x);
    if (g === undefined) continue;
    for (const y of termsB) {
      if (idx.groupOf.get(y) === g) return 'synonym';
    }
  }
  // ② 词集交集（此时必然涉及 aliases）
  const setB = new Set(termsB);
  if (termsA.some((x) => setB.has(x))) return 'synonym';

  const shorter = aName.length <= bName.length ? aName : bName;
  const longer = aName.length <= bName.length ? bName : aName;
  if (shorter.length >= MIN_TOKEN_LEN && longer.includes(shorter)) return 'token';

  return null;
}
