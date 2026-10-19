import React from 'react';
import { XMarkdown } from '@ant-design/x-markdown';
import type { XMarkdownProps } from '@ant-design/x-markdown';
import CiteBadge from './CiteBadge';
import { injectCitationBadges } from './citationIndex';

/** 取包自身的 `components` 值类型 —— 手写一份 props 接口会随包版本漂移 */
type MarkdownComponents = NonNullable<XMarkdownProps['components']>;

/**
 * [jonex] 答案正文渲染器（2026-10-16 接入 Ant Design X）。
 *
 * 取代原先两处各自为政的 `react-markdown` 调用（SessionCard 的主链路、WikiAnswerContent 的
 * Wiki 链路）。选 `@ant-design/x-markdown` 的理由是它为流式设计：按块增量解析并缓存，而
 * react-markdown 在打字机的每个 tick 都要把整篇重新解析一遍（25ms × 全文，总体 O(n²)）。
 *
 * **本组件不定义任何标签样式**，只负责三件事：注入引用角标、装配 XMarkdown、透传流式开关。
 * 排版差异原地保留 —— 主链路的标签样式是 `components` 内联 style（原 SessionCard 的做法），
 * Wiki 链路是 `.answerText` 的 `:global(p)` 后代选择器，两套机制与取值本来就不同，
 * 在这里统一任何一套都会改动另一条链路的观感。
 */

export interface AnswerMarkdownProps {
  /** 正文原文（未注入角标） */
  content: string;
  /** 引用总数：角标编号超过它即降级为不可点 */
  referenceCount?: number;
  /** 当前激活的角标（0-based） */
  activeCitationIndex?: number | null;
  /** 点角标：给出新的激活下标，`null` 表示收起 */
  onCitation?: (index: number | null) => void;
  /** 正文是否仍在产出 —— 决定 XMarkdown 是否处于流式模式 */
  streaming?: boolean;
  /** 标签渲染覆盖，由调用方按自己的排版口径传入；`sup` 始终由本组件接管，不可覆盖 */
  components?: MarkdownComponents;
}

export default function AnswerMarkdown({
  content,
  referenceCount = 0,
  activeCitationIndex = null,
  onCitation,
  streaming = false,
  components,
}: AnswerMarkdownProps) {
  // 角标注入是纯字符串变换，只在正文变化时重算；打字机每 tick 都会变，但代价是一次正则扫描
  const processed = React.useMemo(() => injectCitationBadges(content), [content]);

  const mergedComponents = React.useMemo<MarkdownComponents>(
    () => ({
      ...components,
      // 展开顺序要紧：`sup` 必须最后，否则会被调用方传入的同名项顶掉，角标就退化成普通上标
      sup: (props) => (
        <CiteBadge
          xProps={props}
          referenceCount={referenceCount}
          activeCitationIndex={activeCitationIndex}
          onCitation={onCitation}
        >
          {props.children}
        </CiteBadge>
      ),
    }),
    [components, referenceCount, activeCitationIndex, onCitation],
  );

  return (
    <XMarkdown
      content={processed}
      components={mergedComponents}
      // hasNextChunk 为 false 时 XMarkdown 会 flush 缓存并定稿渲染
      streaming={{ hasNextChunk: streaming }}
    />
  );
}
