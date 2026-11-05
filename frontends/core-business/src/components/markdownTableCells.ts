/**
 * 表格单元格里的"纯符号值"识别（☑/□/✓/✗ 一类）。
 *
 * 背景：知识库检索答案的表格常由模型从参考块的权限矩阵直接抄下来，单元格里是
 * `☑`、`□`、`☑/□` 这样的勾选符号。13.5px 的正文字号下这几对符号的轮廓几乎一样，
 * 用户分不清哪格打了勾，`☑/□` 并排时更难辨。所以渲染层要能把"整格只有一个符号值"
 * 的情况识别出来，换成带颜色的方块（见 `MarkdownTable.tsx` 的 `SymbolCell`）。
 *
 * 判定必须**严格**：只要出现一个普通字符就返回 `null`，让调用方原样渲染。宁可
 * 漏判（符号照旧显示），也不能把普通文本误判成符号（那会把正文的文字吃掉）。
 */

/** 视为「有/已授权/是」的符号 */
const ON_SYMBOLS = new Set(['☑', '✔', '✓', '√', '●', '■']);

/** 视为「无/未授权/否」的符号 */
const OFF_SYMBOLS = new Set(['□', '☐', '☒', '✗', '✘', '×', '○', '◻']);

/** 允许出现在符号之间的分隔符（`☑/□` 的 `/`，全角、竖线、顿号都算） */
const SEPARATORS = new Set(['/', '／', '|', '｜', '、', ',', '，', '·']);

export type SymbolToken =
  | { kind: 'on'; raw: string }
  | { kind: 'off'; raw: string }
  | { kind: 'sep'; raw: string };

/**
 * 把整格符号文本拆成 token；不是"纯符号单元格"时返回 `null`。
 *
 * ```ts
 * splitSymbolCell('☑/□') // [{kind:'on'}, {kind:'sep'}, {kind:'off'}]
 * splitSymbolCell('是')  // null
 * ```
 *
 * 空白（含全角空格、换行）直接跳过，不计入判定。
 */
export function splitSymbolCell(text: string): SymbolToken[] | null {
  const tokens: SymbolToken[] = [];
  let symbolCount = 0;
  // 用 Array.from 而不是 for...of 索引：☑/□ 都在 BMP 内，但保持按码点遍历的写法，
  // 万一模型输出 ✔️ 这类带变体选择符的符号，也不会被拆成半个字符。
  for (const ch of Array.from(text)) {
    if (ON_SYMBOLS.has(ch)) {
      tokens.push({ kind: 'on', raw: ch });
      symbolCount += 1;
    } else if (OFF_SYMBOLS.has(ch)) {
      tokens.push({ kind: 'off', raw: ch });
      symbolCount += 1;
    } else if (SEPARATORS.has(ch)) {
      tokens.push({ kind: 'sep', raw: ch });
    } else if (/^\s$/.test(ch) || ch === '\uFE0F') {
      // 空白与 emoji 变体选择符：忽略，不参与判定
      continue;
    } else {
      return null; // 出现任何普通字符 ⇒ 不是纯符号单元格
    }
  }
  return symbolCount > 0 ? tokens : null;
}
