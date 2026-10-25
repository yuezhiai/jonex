/**
 * 多源视图的实体节点：**只读**。与编辑画布的 EntityNode 同款外形
 * （头部名字 + 属性预览 6 条），加了来源徽章与所属概念行；拖拽/连线/展开
 * 一概不挂——只读先行，属性展开要看请回单源画布。
 */
import type { ReactNode } from 'react';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';

import { AttributeRow, splitNodeAttributes } from '../components/entityAttributes.js';
import type { MultiSourceEntity } from './model.js';

export interface MultiSourceNodeData extends Record<string, unknown> {
  entity: MultiSourceEntity;
  /** 源色（与区域框同色） */
  color: string;
}

export type MultiSourceNodeType = Node<MultiSourceNodeData, 'msEntity'>;

export function MultiSourceEntityNode({ data }: NodeProps<MultiSourceNodeType>): ReactNode {
  const { entity, color } = data;
  const { visible, rest } = splitNodeAttributes(entity.attributes);

  return (
    <div className="ms-node">
      <Handle type="target" position={Position.Left} />
      <div className="ms-node-header">
        <strong>{entity.displayName || entity.name}</strong>
        <span className="ms-node-badge" style={{ background: color }}>{entity.sourceName}</span>
      </div>
      <code className="ms-node-code">{entity.name}</code>
      <div className="ms-node-attributes">
        {visible.length
          ? visible.map((attribute) => <AttributeRow key={attribute.id} attribute={attribute} />)
          : <span className="node-empty">—</span>}
      </div>
      {rest.length > 0 ? <div className="ms-node-more">{`+${rest.length}`}</div> : null}
      {entity.concepts.length ? (
        <div className="ms-node-concepts">
          {entity.concepts.map((name) => <span key={name} className="ms-node-concept">{name}</span>)}
        </div>
      ) : null}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
