export type DocumentStageKey = 'parse' | 'quality' | 'compile';

/**
 * 文档详情页的 Tab 顺序。
 *
 * 质量报告**不能**与解析结果同处一个纵向 flex：报告卡高度随指标表/摘要变化，
 * 会挤占或顶掉片段查看器；无报告时的空态块也会把原文片段往下推。
 * 本版本不接入解析质量观测，恒返回解析 + 编译两个 Tab。
 */
export function buildDocumentStageKeys(): DocumentStageKey[] {
  return ['parse', 'compile'];
}
