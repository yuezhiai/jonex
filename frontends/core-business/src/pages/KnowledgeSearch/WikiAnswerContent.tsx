import React, { useMemo } from 'react';
import AnswerMarkdown from './AnswerMarkdown';
import { formatWikiAnswer } from './wikiPresentation';
import styles from './WikiSearch.module.scss';

/**
 * Wiki 链路的答案正文。
 *
 * 只做两件 Wiki 专属的事：走 `formatWikiAnswer` 预处理、套 `.answerText` 的排版口径
 * （它用 `:global(p)` 一类的后代选择器给 markdown 元素定样式）。引用角标、流式装配
 * 都交给 `AnswerMarkdown`，与主链路共用同一套逻辑。
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
        components={{
          table: ({ children }) => (
            <div className={styles.answerTable}>
              <table>{children}</table>
            </div>
          ),
        }}
      />
    </div>
  );
}
