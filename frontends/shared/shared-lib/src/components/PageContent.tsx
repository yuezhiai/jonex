import React from 'react';
import { Card } from 'antd';
import type { CardProps } from 'antd';

/** 主体高度怎么算：铺满剩余高度（内部滚动），还是由内容撑开（页面整体滚动）。 */
export type PageContentHeight = 'fill' | 'content';

/** antd 的 styles 允许传函数；页面外壳只接静态对象，才能和默认 body 样式合并。 */
type StaticStyles<T> = T extends (...args: never[]) => unknown ? never : T;

export interface PageContentProps {
  /** 页面主体内容。 */
  children?: React.ReactNode;
  /** 是否套白色卡片外壳，默认 true。false 走裸容器：无白底/边框/圆角/内边距。 */
  showCard?: boolean;
  /**
   * 高度模式，默认 `fill`。
   *
   * - `fill`：吃掉页头之下的剩余高度，内容超出时在主体内部滚动。表头/分页固定的列表页用这个。
   * - `content`：不抢高度，由内容撑开，滚动交给外层 `.yx-content`。行数不多、又不希望页面里
   *   再套一层滚动条的页面用这个（例如日志管理：一页 10 行，内部滚动反而多一个滚动容器）。
   */
  height?: PageContentHeight;
  /** 覆盖默认样式，与对应模式的默认样式浅合并。 */
  style?: React.CSSProperties;
  /** 追加到卡片（或裸容器）根节点的类名，供页面级作用域样式使用。 */
  className?: string;
  /**
   * 卡片各区块样式，与 antd Card 的 `styles` 同构；`body` 会与本组件的默认 body 样式浅合并。
   *
   * 典型用法：页签栏贴卡片顶边、留白交给内容区自己给的页面传
   * `{ body: { padding: 0 } }`。裸容器没有卡片结构，忽略。
   */
  styles?: StaticStyles<NonNullable<CardProps['styles']>>;
}

/**
 * 铺满模式：需要父级是 flex column（.yx-content 已提供）才能吃掉剩余高度；
 * minHeight 解除 flex item 的 auto 下限，内容超出时才能收缩而非撑破父级。
 */
const LAYOUT_STYLE: React.CSSProperties = {
  flex: '1 1 auto',
  minHeight: 0,
  display: 'flex',
  flexDirection: 'column',
};

/**
 * 内容撑开模式：`flex: 0 0 auto` 让它按内容定高，父级 `.yx-content` 的 overflow-y 接管滚动。
 * 不能再带 `minHeight: 0`，否则 flex 容器仍会把子项压扁。
 */
const CONTENT_LAYOUT_STYLE: React.CSSProperties = {
  flex: '0 0 auto',
  display: 'flex',
  flexDirection: 'column',
};

/** 圆角跟主题走，不自己定值——否则调 --yx-card-radius 时这张主体卡片会掉队。 */
const CARD_STYLE: React.CSSProperties = {
  ...LAYOUT_STYLE,
  borderRadius: 'var(--yx-card-radius)',
};

const CARD_CONTENT_STYLE: React.CSSProperties = {
  ...CONTENT_LAYOUT_STYLE,
  borderRadius: 'var(--yx-card-radius)',
};

/** 卡片模式下滚动由 .ant-card-body 承担，裸容器没有那一层，改由自身滚。 */
const PLAIN_STYLE: React.CSSProperties = {
  ...LAYOUT_STYLE,
  overflow: 'auto',
};

/** 裸容器 + 内容撑开：不自己滚（父级滚），也不吃剩余高度。 */
const PLAIN_CONTENT_STYLE: React.CSSProperties = {
  ...CONTENT_LAYOUT_STYLE,
};

/** 撑满卡片并自行滚动，内容不会溢出卡片边框。 */
const BODY_STYLE: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflow: 'auto',
};

/** 页面主体：默认是不带标题的 Card，铺满页头之下的剩余高度。 */
const PageContent: React.FC<PageContentProps> = ({
  children,
  showCard = true,
  height = 'fill',
  style,
  className,
  styles,
}) => {
  const fill = height === 'fill';

  if (!showCard) {
    return (
      <div className={className} style={{ ...(fill ? PLAIN_STYLE : PLAIN_CONTENT_STYLE), ...style }}>
        {children}
      </div>
    );
  }

  // 铺满模式有默认 body 布局（flex + 内部滚动），调用方的 body 样式叠加在它之上
  const bodyStyle = fill ? { ...BODY_STYLE, ...styles?.body } : styles?.body;
  const mergedStyles = bodyStyle ? { ...styles, body: bodyStyle } : styles;

  return (
    <Card className={className} style={{ ...(fill ? CARD_STYLE : CARD_CONTENT_STYLE), ...style }} styles={mergedStyles}>
      {children}
    </Card>
  );
};

export default PageContent;
