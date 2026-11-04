/** 数据答案正文的展示裁剪（router 把「总结 + 数据引用」拼在同一个 answer 里）。 */

/** router 追加的引用段标题；与 `onto-intelligent/src/router.py` 的 `_compose_data_answer` 同字面量。 */
export const DATA_REFERENCE_MARKER = '**数据引用**';

/**
 * 有结构化数据块时，把正文里的「数据引用」段切掉 —— `DataBlocksView` 已经渲染了
 * 真表格与折叠 SQL，正文再贴一遍就是重复。
 *
 * ⚠️ 判据必须是**有块**：老后端只发文本、不带 `data_blocks`，这时切掉等于把表格
 * 与 SQL 一起丢掉。没有标记（RAG / 本体 / 老答案）一律原样返回。
 */
export function stripDataReference(text: string, hasBlocks: boolean): string {
  if (!hasBlocks) return text;
  const i = text.indexOf(DATA_REFERENCE_MARKER);
  return i === -1 ? text : text.slice(0, i).trimEnd();
}
