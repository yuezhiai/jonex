import React from 'react';
import { useTranslation } from 'react-i18next';
import { CITE_BADGE_CLASS, parseCiteIndex } from './citationIndex';
import styles from './Citation.module.scss';

/**
 * [jonex] 答案正文里的引用角标（2026-10-16 接入 Ant Design X）。
 *
 * 由 `AnswerMarkdown` 经 XMarkdown 的 `components.sup` 挂载，接收的是角标元素本身的
 * 属性与子节点。主链路（SessionCard）与 Wiki 链路（WikiAnswerContent）共用本组件，
 * 避免两处判定逻辑再次漂移。
 */

/**
 * XMarkdown 交给自定义组件的 props 形态在文档里只给了 `{ domNode, streamStatus, children, rest }`，
 * 但 `rest` 是否已把 `class` 转成 `className`、属性是挂在 `rest` 上还是 `domNode.properties` 上，
 * 都取决于内部实现（该组件在 SSR 下不产出内容，无法用静态标记探针确认）。
 * 与其赌一种形态，这里按多路依次查找，命中任一即可。
 */
function pickAttr(xProps: unknown, name: string): unknown {
  const sources = [xProps, (xProps as any)?.rest, (xProps as any)?.domNode?.properties, (xProps as any)?.domNode?.attributes];
  for (const source of sources) {
    if (source && typeof source === 'object' && name in source) return (source as Record<string, unknown>)[name];
  }
  return undefined;
}

function hasBadgeClass(value: unknown): boolean {
  if (Array.isArray(value)) return value.includes(CITE_BADGE_CLASS);
  if (typeof value === 'string') return value.split(/\s+/).includes(CITE_BADGE_CLASS);
  return false;
}

/** 递归取 React 节点的纯文本 —— `data-cite-index` 取不到时用它兜底解析编号 */
function textOf(node: React.ReactNode): string {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (React.isValidElement(node)) return textOf((node.props as { children?: React.ReactNode }).children);
  return '';
}

export function isCiteBadge(xProps: unknown): boolean {
  return hasBadgeClass(pickAttr(xProps, 'className')) || hasBadgeClass(pickAttr(xProps, 'class'));
}

export interface CiteBadgeProps {
  /** XMarkdown 传给自定义组件的原始 props */
  xProps: unknown;
  children?: React.ReactNode;
  /** 引用总数：编号超过它说明后端没回填这条引用，降级为不可点 */
  referenceCount: number;
  /** 当前激活的角标（0-based，对应 references 下标） */
  activeCitationIndex: number | null;
  /** 点角标：给出新的激活下标，`null` 表示收起面板（再点一次同一角标） */
  onCitation?: (index: number | null) => void;
}

export default function CiteBadge({ xProps, children, referenceCount, activeCitationIndex, onCitation }: CiteBadgeProps) {
  const { t } = useTranslation();

  // 正文里真正的上标（非引用角标）原样渲染，不套角标样式
  if (!isCiteBadge(xProps)) return <sup>{children}</sup>;

  const index = parseCiteIndex(pickAttr(xProps, 'data-cite-index') ?? textOf(children));
  // 编号越界 / 解析失败 → 降级为不可点的静态角标，保持改动前的纯展示形态
  const valid = index != null && index <= referenceCount;
  if (!valid) {
    return (
      <sup className={styles.badgeSup}>
        <span className={styles.badgeStatic}>{children}</span>
      </sup>
    );
  }

  const active = activeCitationIndex === index - 1;
  return (
    <sup className={styles.badgeSup}>
      <button
        type="button"
        className={`${styles.badge} ${active ? styles.badgeActive : ''}`}
        aria-label={t('knowledgeSearch.citationBadgeAria', { index })}
        aria-current={active ? 'true' : undefined}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          // 再点一次同一角标收起面板
          onCitation?.(active ? null : index - 1);
        }}
      >
        {children}
      </button>
    </sup>
  );
}
