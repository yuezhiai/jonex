import type { ReactNode } from 'react';
// 图标统一用 Jonex 已有的 @ant-design/icons（原 nexonto 用 lucide-react）
import { KeyOutlined } from '@ant-design/icons';
import type { Attribute, EntityType } from '../domain/index.js';

/** 画布节点上预览的属性条数:超出部分折成一条「+N」行,悬停该行续出其余属性。
 *  单独成模块(而非留在 ModelCanvas.tsx)是为了可测:ModelCanvas 依赖 @xyflow/react 与 createPortal,
 *  纯 Node 静态渲染跑不动;这里只留属性行的展示逻辑。 */
export const NODE_ATTRIBUTE_PREVIEW = 6;

/** 节点可见属性与「未显示」的其余属性。
 *  纯函数单独导出:展开态是组件内部 state,renderToStaticMarkup 驱动不了交互(与 DecisionCard
 *  的 selectedOptionDestructive 同款拆法),故把切片判断拆出来单测。
 *  rest 返回数组而非计数:展开浮层要的正是这批「还没显示过的」属性。 */
export function splitNodeAttributes(attributes: readonly Attribute[]): { visible: readonly Attribute[]; rest: readonly Attribute[] } {
  return { visible: attributes.slice(0, NODE_ATTRIBUTE_PREVIEW), rest: attributes.slice(NODE_ATTRIBUTE_PREVIEW) };
}

/** 属性行:主键图标 + 名 + 类型。节点内与展开浮层共用一份,保证两处字形/配色完全一致。 */
export function AttributeRow({ attribute, added }: { attribute: Attribute; added?: boolean }): ReactNode {
  return (
    <div className={added === true ? 'entity-attr-added' : undefined}>
      <span>{attribute.primaryKey ? <KeyOutlined style={{ fontSize: 11 }} /> : null}{attribute.name}</span>
      <small>{attribute.type}</small>
    </div>
  );
}

/** 展开浮层的内容:**只**渲染节点上没显示过的属性行(不重复前 NODE_ATTRIBUTE_PREVIEW 条),
 *  加上「与节点同宽、顶边无缝」的样式,读起来就是节点向下长出了几行。
 *  定位/开合由 ModelCanvas 负责,这里只管内容 —— 因此可以脱离画布做静态渲染测试。 */
export function ExpandedAttributeRows({ attributes }: { attributes: readonly Attribute[] }): ReactNode {
  return <>{attributes.map((attribute) => <AttributeRow key={attribute.id} attribute={attribute} />)}</>;
}
