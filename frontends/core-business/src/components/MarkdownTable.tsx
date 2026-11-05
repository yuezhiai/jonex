import React from 'react';
import styles from './MarkdownTable.module.scss';
import { splitSymbolCell, type SymbolToken } from './markdownTableCells';

/**
 * 答案 / 文档里的 Markdown 表格（GFM `| a | b |`）统一渲染件。
 *
 * 为什么需要它：平台里有 3 条链路在渲染 markdown（检索答案 `AnswerMarkdown`、
 * Wiki 答案、引用详情 `MarkdownContent`），表格原先三套口径 —— 主链路干脆没有
 * `table` 覆盖，落到浏览器默认样式（无边框、无内边距、宽了顶破容器）；Wiki 侧自己
 * 写了一份 `.answerTable`。这里收成一处，三条链路共用：
 *
 * - 外面套可横向滚动的圆角容器（窄屏 / 抽屉打开时不再撑破布局），容器可聚焦；
 * - 表头浅底、行斑马纹、单元格 14px 内边距、首列粘左；
 * - 单元格是 `☑/□` 一类纯符号时换成色块（见 `SymbolCell`），13.5px 正文下也能分辨。
 *
 * 注意：`@ant-design/x-markdown` 自带的表格样式在 `themes/light.css` 里，需要额外
 * 引入才会生效，本应用没有引（会顺带改掉整篇答案的排版），所以样式自带一份。
 */

/** 勾选符号单元格：`☑` 绿底对勾、`□` 空方块，中间的分隔符细灰 */
export function SymbolCell({ tokens, raw }: { tokens: SymbolToken[]; raw: string }) {
  return (
    // title 保留模型原文，悬停仍能看到它到底写了什么（不改动答案内容）
    <span className={styles.symbolCell} title={raw}>
      {tokens.map((token, i) => {
        if (token.kind === 'sep') {
          return (
            <span key={i} className={styles.symbolSep}>
              {token.raw}
            </span>
          );
        }
        return (
          <span
            key={i}
            role="img"
            aria-label={token.kind === 'on' ? '已勾选' : '未勾选'}
            className={`${styles.symbolBox} ${
              token.kind === 'on' ? styles.symbolOn : styles.symbolOff
            }`}
          />
        );
      })}
    </span>
  );
}

/**
 * 单元格内容出口：纯符号文本换成 `SymbolCell`，其余原样返回。
 *
 * 只处理 `children` 是**字符串**的情况 —— 单元格里有行内 markdown（`**粗**`、
 * `[1]` 角标）时 `children` 已是元素，不参与判定，避免误伤。
 */
export function markdownTableCell(children: React.ReactNode): React.ReactNode {
  if (typeof children !== 'string') return children;
  const tokens = splitSymbolCell(children);
  if (!tokens) return children;
  return <SymbolCell tokens={tokens} raw={children.trim()} />;
}

export default function MarkdownTable({ children }: { children?: React.ReactNode }) {
  return (
    <div className={styles.wrap} tabIndex={0} role="group" aria-label="表格（可横向滚动）">
      <table className={styles.table}>{children}</table>
    </div>
  );
}
