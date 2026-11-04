import React, { useMemo } from 'react';
import AnswerMarkdown from './AnswerMarkdown';
import { formatWikiAnswer } from './wikiPresentation';
import styles from './WikiSearch.module.scss';

/**
 * Wiki 链路的答案正文。
 *
 * 只做两件 Wiki 专属的事：走 `formatWikiAnswer` 预处理、套 `.answerText` 的排版口径
 * （它用 `:global(p)` 一类的后代选择器给 markdown 元素定样式）。引用角标、流式装配、
 * 表格渲染都交给 `AnswerMarkdown`，与主链路共用同一套逻辑 —— 表格原先在 Wiki 侧单独
 * 覆盖过一份 `.answerTable`，现已并入 `@/components/MarkdownTable`，故此处不再传 `components`。
 */
export default function WikiAnswerContent({ content, referenceCount, activeIndex, onCitation }: {
  content: string;
  referenceCount: number;
  activeIndex: number | null;
  /** 点角标：给出新的激活下标，`null` 表示收起 */
  onCitation: (index: number | null) => void;
}) {
  const formatted = useMemo(() => formatWikiAnswer(content), [content]);

  return (
    <div className={styles.answerText}>
      <AnswerMarkdown
        content={formatted}
        referenceCount={referenceCount}
        activeCitationIndex={activeIndex}
        onCitation={onCitation}
      />
    </div>
  );
}
