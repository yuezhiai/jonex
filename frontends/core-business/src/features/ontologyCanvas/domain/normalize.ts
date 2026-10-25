/**
 * 名称归一化。
 *
 * 原本住在 `@nexonto/domain` 的 `proposal.ts`（AI 提案模块）里，而本移植
 * **不带 AI 提案**。但 `commands.ts` 与 `duplicates.ts` 都依赖它做重名判定，
 * 所以摘出来独立成文件，而不是把整个提案模块拖进来。
 */

/** 技术名归一：NFC 规范化 +（可选）大小写不敏感。重名判定一律走它。 */
export function normalizeTechnicalName(value: string, caseInsensitive: boolean): string {
  const normalized = value.normalize('NFC');
  return caseInsensitive ? normalized.toLocaleLowerCase('en-US') : normalized;
}
