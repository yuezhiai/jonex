/**
 * 血缘来源档 → 人话 i18n 键（与 `LineageTab` 的 `ORIGIN_TAGS` 同源）。
 *
 * 影响分析视图、覆盖率条、来源档下拉都从这里取，避免各自维护一份并在
 * UI 上漏出原始英文档位（如 `platform: 4`）。未知档位原样显示：不猜、也不吞。
 */
export const ORIGIN_LABEL_KEYS: Record<string, string> = {
  declared: 'domainKnowledge.lineage.originDeclared',
  manual: 'domainKnowledge.lineage.originManual',
  comment: 'domainKnowledge.lineage.originComment',
  platform: 'domainKnowledge.lineage.originPlatform',
  value: 'domainKnowledge.lineage.originValue',
  derived: 'domainKnowledge.lineage.originDerived',
  // 覆盖率条的合成桶（空串 origin 记为 unknown），不显示原始英文。
  unknown: 'common.unknown',
};

/** 取来源档的人话标签；未知档回落到原始字符串。 */
export function originLabel(t: (key: string) => string, origin: string): string {
  const key = ORIGIN_LABEL_KEYS[origin];
  return key ? t(key) : origin;
}
