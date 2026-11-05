import type { TFunction } from 'i18next';
import type { PatchEntry, SkipItem } from '../../api/evolution';

const ALIAS_TAKEN = 'alias_taken_by:';
const COLUMN_HAS_RULE = /^该列已有 query_rule（(.*)）——先决定是否改写它$/;
const norm = (v?: string) => String(v ?? '').trim();

/** relation 条目（`{source, source_column, target, target_column}`）的两端展示：
 * `source.col → target.col`；非 relation 形状返回 null。 */
type RelationShape = {
  source?: unknown;
  source_column?: unknown;
  target?: unknown;
  target_column?: unknown;
};

const colOf = (v: unknown) => (v === undefined || v === null ? '?' : String(v));

export const relationEdgeOf = (e: RelationShape): string | null => {
  if (e.source === undefined || e.target === undefined) return null;
  return `${String(e.source)}.${colOf(e.source_column)} → ${String(e.target)}.${colOf(e.target_column)}`;
};

/** 条目展示名：alias 形状 → `concept ＋ 「alias」`；query_rule 形状 → 裸 target_code；
 * relation 形状 → `source.col → target.col`。
 *
 * 跳过条目（`skipped[]`）的 relation 形状是**残缺**的：后端按最早失败的字段就停，
 * 可能只带 source 或只带 target（`{index, source, why}`），也可能只有边名
 * （`{index, name, why}`）。逐级兜底，别渲染成空白 `<code>`。 */
export function skipItemLabel(s: SkipItem): string {
  const c = String(s.concept ?? '').trim();
  if (c !== '') return `${c} ＋ 「${s.alias ?? ''}」`;
  // synonym 跳过条目：`{terms, canonical, why}`（没有 concept / 边 / target_code）；
  // terms 可能为空（empty_terms：整组没有可用词）—— 空词组也要显示，不能渲染成空白。
  if (Array.isArray(s.terms)) {
    const terms = s.terms.map((term) => String(term ?? '').trim()).filter(Boolean);
    return `${terms.join(' / ') || '-'} → ${String(s.canonical ?? '').trim()}`;
  }
  const edge = relationEdgeOf(s);
  if (edge !== null) return edge;
  if (s.source !== undefined) return `${String(s.source)}.${colOf(s.source_column)} → ?`;
  if (s.target !== undefined) return `? → ${String(s.target)}.${colOf(s.target_column)}`;
  const name = String(s.name ?? '').trim();
  if (name !== '') return name;
  return s.index !== undefined ? `#${s.index}` : String(s.target_code ?? '');
}

/**
 * skip 条目 → 人话（映射表与 evolve.py `_skip_text` 逐字对齐）。
 * `opts.apply=true`（预览弹窗）：这一列全是落库期跳过 → already_present 恒为「库里已有」；
 * 草稿期（默认）看 (concept, alias) 是否 ∈ entries —— ∈ 为「批内重复」，∉ 为「库里已有」。
 * 服务器已带完整句子的格式化码（实体不在/没列/已有约束）原样透出。
 */
export function humanizeSkipReason(
  t: TFunction, s: SkipItem, entries: PatchEntry[], opts: { apply?: boolean } = {},
): string {
  const why = String(s.why ?? '');
  if (why.startsWith(ALIAS_TAKEN)) {
    return t('evolutionConsole.skipWhyAliasTaken', { by: why.slice(ALIAS_TAKEN.length) });
  }
  if (why === 'already_present') {
    if (!opts.apply && entries.some(
      (e) => norm(e.concept) === norm(s.concept) && norm(e.alias) === norm(s.alias))) {
      return t('evolutionConsole.skipWhyBatchDup');
    }
    return t('evolutionConsole.skipWhyInDb');
  }
  // synonym 批的落库期跳过码（adopt_common._plan_synonym_entries / apply 阶段）。
  if (why === 'empty_terms') return t('evolutionConsole.skipWhySynonymEmptyTerms');
  if (why === 'empty_canonical') return t('evolutionConsole.skipWhySynonymEmptyCanonical');
  if (why === 'exists' && Array.isArray(s.terms)) {
    return t('evolutionConsole.skipWhySynonymExists');
  }
  if (why === 'not_found' && Array.isArray(s.terms)) {
    return t('evolutionConsole.skipWhySynonymNotFound');
  }
  if (why === 'cross_group_duplicate' && Array.isArray(s.terms)) {
    return t('evolutionConsole.skipWhySynonymCrossGroupDuplicate', {
      term: String(s.term ?? '').trim() || '-',
    });
  }
  if (why === 'unknown_op') {
    return t('evolutionConsole.skipWhySynonymUnknownOp');
  }
  if (why === 'concept_not_found') return t('evolutionConsole.skipWhyConceptMissing');
  if (why === 'bad_args') return t('evolutionConsole.skipWhyBadArgs');
  if (why === 'tools_full') return t('evolutionConsole.skipWhyToolsFull');
  if (why === 'expression 为空') return t('evolutionConsole.skipWhyExprEmpty');
  if (why.startsWith('target_code 要写成')) {
    const code = why.split('：').slice(1).join('：').trim().replace(/^["']+|["']+$/g, '');
    return t('evolutionConsole.skipWhyTargetFormat', { code });
  }
  if (why === '候选文件里没有这个 target_code') return t('evolutionConsole.skipWhyTargetMissingFile');
  const m = why.match(COLUMN_HAS_RULE);
  if (m) return t('evolutionConsole.skipWhyColumnHasRule', { name: m[1] });
  if (why.startsWith('该列已有 query_rule')) return why.replace('该列已有 query_rule', '该列已有查询规则');
  return why || t('evolutionConsole.skipWhyUnknown');
}
