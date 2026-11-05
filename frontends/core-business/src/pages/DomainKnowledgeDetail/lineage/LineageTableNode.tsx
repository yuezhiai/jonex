/**
 * 血缘画布的自定义节点（`type: 'lineageTable'`）：**只读**。
 *
 * 展示三层：实体名（粗体，缺省回落到物理表名）→ 物理表名（等宽）→ 属性数。
 * 锚点节点（`data.depth === 0`，深度由 `buildLineageGraph` 写入）用蓝色描边 +
 * 光晕高亮，其余节点是普通灰边白底 —— 视觉上「从哪张表展开」一眼可见。
 *
 * ⚠️ 两侧 Handle **恒同时渲染**：画布边一律按数据流方向 `from → to` 画，
 * 上游展开（`direction=upstream`）时边仍从左表指向右表，所以同一节点既可能
 * 是某条边的 source、也可能是另一条的 target —— 按方向条件渲染会丢连线。
 * 画布只读，Handle 一并 `isConnectable={false}`。
 *
 * 样式全部内联：节点挂在 React Flow 的 transform 层里，不碰全局样式表，
 * 也不依赖 `features/ontologyCanvas` 的 scss（两个画布互不耦合）。
 */
import type { ReactNode } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { useTranslation } from 'react-i18next';

import { LINEAGE_NODE_HEIGHT, LINEAGE_NODE_WIDTH, type LineageRFNode } from './lineageGraphModel';

/** 节点内边距（左右 12、上下 8）；尺寸与图模型的 240×80 对齐，层/行不会重叠。 */
const NODE_STYLE = {
  width: LINEAGE_NODE_WIDTH,
  height: LINEAGE_NODE_HEIGHT,
  padding: '8px 12px',
  boxSizing: 'border-box',
  display: 'flex',
  flexDirection: 'column',
  justifyContent: 'center',
  gap: 2,
  borderRadius: 8,
  background: '#ffffff',
  border: '1px solid #d9e2ef',
  fontFamily: 'inherit',
} as const;

/** 锚点高亮：蓝色描边 + 外圈光晕（与列表页「手工登记」蓝色同一系）。 */
const ANCHOR_STYLE = {
  border: '2px solid #1677ff',
  boxShadow: '0 0 0 3px rgba(22, 119, 255, 0.16)',
  background: '#eff6ff',
} as const;

const TITLE_STYLE = {
  fontSize: 13,
  fontWeight: 600,
  lineHeight: '18px',
  color: '#0b2b5c',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
} as const;

const TABLE_STYLE = {
  fontSize: 11,
  lineHeight: '14px',
  color: '#64748b',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
} as const;

const META_STYLE = {
  fontSize: 11,
  lineHeight: '14px',
  color: '#94a3b8',
} as const;

export function LineageTableNode({ data }: NodeProps<LineageRFNode>): ReactNode {
  const { t } = useTranslation();
  const isAnchor = data.depth === 0;
  // 有实体名时标题用实体名、物理表名单独一行（等宽）；没有实体名时标题就是表名，
  // 再单独渲染一遍表名只会是重复噪音。
  const title = data.entityName || data.table;
  const showTableCode = Boolean(data.entityName) && data.entityName !== data.table;

  return (
    <div
      style={{ ...NODE_STYLE, ...(isAnchor ? ANCHOR_STYLE : null), cursor: 'pointer' }}
      title={isAnchor ? t('domainKnowledge.lineage.graph.hint') : undefined}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div style={TITLE_STYLE} title={title}>
        {title}
      </div>
      {showTableCode ? (
        <code style={TABLE_STYLE} title={data.table}>
          {data.table}
        </code>
      ) : null}
      <div style={META_STYLE}>
        {t('domainKnowledge.lineage.graph.attributeCount', { count: data.attributeCount })}
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}
