/**
 * 本体画布（React Flow）。
 *
 * 移植自 nexonto 的 `ModelCanvas.tsx`，砍掉了三块：
 *   - `addedMark` / `addedEntityIds` / `addedRelationIds` —— 抽取与 AI 合入时的绿色「新增」高亮
 *   - `onEntityClick` / `onEntityTarget` —— 与 AI 对话侧的高亮联动
 *   - `viewport.set` —— 视口持久化（本移植不存编辑器状态）
 *
 * 节点坐标来自 `layout.ts` 的自动布局，在**文档初始化之前**算好写进 `nodePositions`，
 * 因此它不属于可撤销的编辑动作——撤销不该把布局撤掉。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  Controls, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider,
  useNodesState, useReactFlow, type Connection, type Edge, type Node, type NodeProps,
} from '@xyflow/react';
import { useTranslation } from 'react-i18next';

import { newId, type AttributeType, type DomainCommand, type DomainDocument, type EntityType } from '../domain/index.js';
import type { Selection } from '../store/editorStore.js';
import { AttributeRow, ExpandedAttributeRows, splitNodeAttributes } from './entityAttributes.js';
import { buildSourceColorMap, entitySourceBadges } from './entitySources.js';

interface EntityNodeData extends Record<string, unknown> {
  entity: EntityType;
  /** 文档级来源色映射（抽象态才有落点；编译态为空 map → 徽章恒空） */
  sourceColors: ReadonlyMap<string, string>;
}
type EntityNodeType = Node<EntityNodeData, 'entity'>;

/** 实体节点：属性只预览前 N 条，余下折成一条「+N」；悬停该行，节点下方续出剩下的属性行。
 *
 *  浮层走 portal 渲染进 document.body —— 两条 CSS 约束逼出来的：`.entity-node` 是
 *  `overflow:hidden`（会裁剪子元素），而画布 viewport 带 transform（transform 祖先会成为
 *  `position:fixed` 的包含块，fixed 退化成跟随画布缩放）。只有脱离这棵子树才能既不被裁剪、
 *  又不随缩放变形。浮层不改变节点尺寸 → 不挤动布局、不移动连线锚点。 */
function EntityNode({ data, selected }: NodeProps<EntityNodeType>): ReactNode {
  const { t } = useTranslation();
  const nodeRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | undefined>(undefined);
  const [anchor, setAnchor] = useState<{ left: number; top: number; width: number; maxHeight: number } | undefined>(undefined);
  const { visible, rest } = splitNodeAttributes(data.entity.attributes);
  const badges = entitySourceBadges(data.entity, data.sourceColors);

  const cancelClose = useCallback(() => {
    if (closeTimer.current !== undefined) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = undefined;
    }
  }, []);

  const openPanel = useCallback(() => {
    cancelClose();
    const rect = nodeRef.current?.getBoundingClientRect();
    if (!rect) return;
    const top = rect.bottom - 1;
    setAnchor({ left: rect.left, top, width: rect.width, maxHeight: Math.max(88, window.innerHeight - top - 8) });
  }, [cancelClose]);

  /** 延迟关闭：留出鼠标从徽标移进浮层的时间（浮层挂在 body，与徽标没有 DOM 父子关系，不能靠冒泡续命） */
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = undefined;
      setAnchor(undefined);
    }, 160);
  }, [cancelClose]);

  useEffect(() => () => cancelClose(), [cancelClose]);

  // 固定定位的浮层在画布缩放/平移后会失锚 → 滚轮、浮层外按下、Esc 一律收起
  useEffect(() => {
    if (anchor === undefined) return;
    const onWheel = (): void => setAnchor(undefined);
    // composedPath 而非 contains：本文件从 @xyflow/react 导入了同名类型 `Node`，
    // 会把 DOM 的 Node 盖掉；路径数组比较同时也对 portal 结构更稳
    const onPointerDown = (event: PointerEvent): void => {
      if (panelRef.current !== null && !event.composedPath().includes(panelRef.current)) setAnchor(undefined);
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') setAnchor(undefined);
    };
    window.addEventListener('wheel', onWheel, { passive: true });
    window.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('wheel', onWheel);
      window.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [anchor]);

  return (
    <div ref={nodeRef} className={`entity-node ${selected ? 'entity-node-selected' : ''}${anchor !== undefined ? ' entity-node-expanded' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <div className="entity-node-header">
        <strong>{data.entity.displayName || data.entity.name}</strong>
        <code>{data.entity.name}</code>
        {badges.length ? (
          // 徽章只呈现在**节点**上：included 落点的源名（颜色按文档级映射）。
          // 原生 title 而非 antd Tooltip：画布 viewport 带 transform，portal 类浮层易失锚。
          <div className="entity-node-sources">
            {badges.map((badge) => (
              <span key={badge.dataSourceId} className="entity-node-source" style={{ background: badge.color }} title={badge.label}>
                {badge.label}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <div className="entity-node-attributes">
        {visible.length
          ? visible.map((attribute) => <AttributeRow key={attribute.id} attribute={attribute} />)
          : <span className="node-empty">—</span>}
      </div>
      {rest.length > 0 ? (
        <button
          type="button"
          className="entity-node-more"
          aria-expanded={anchor !== undefined}
          aria-label={t('editor.attrMore', { n: rest.length })}
          onMouseEnter={openPanel}
          onMouseLeave={scheduleClose}
          onFocus={openPanel}
          onBlur={scheduleClose}
        >{`+${rest.length}`}</button>
      ) : null}
      <Handle type="source" position={Position.Right} />
      {anchor !== undefined ? createPortal(
        <div
          ref={panelRef}
          className="entity-attr-panel"
          style={{ left: anchor.left, top: anchor.top, width: anchor.width, maxHeight: anchor.maxHeight }}
          onMouseEnter={cancelClose}
          onMouseLeave={scheduleClose}
        >
          <ExpandedAttributeRows attributes={rest} />
        </div>,
        document.body,
      ) : null}
    </div>
  );
}

const nodeTypes = { entity: EntityNode };

/** 首屏缩放：节点宽 220px，0.75 下约 165px——正文可读，又不至于一屏只放得下两三个实体。 */
const READABLE_ZOOM = 0.75;

interface CanvasProps {
  document: DomainDocument;
  selection: Selection;
  execute: (command: DomainCommand, selection?: Selection) => void;
  select: (selection: Selection) => void;
  addEntity: () => void;
  addAttribute: (entityId: string, type: AttributeType) => void;
  /** 递增即重新取景（用户点了「重新布局」）。跨 Provider 传不进来回调，所以用信号。 */
  relayoutTick?: number;
  /** 全只读（抽象 schema 处于 active 时传入；编译画布不传 = 现状）。
   *  关掉拖拽/连线/拖入新建，**保留选中与平移缩放**——只读仍要能看。 */
  readOnly?: boolean;
}

function CanvasInner({ document, selection, execute, select, addEntity, addAttribute, relayoutTick = 0, readOnly = false }: CanvasProps): ReactNode {
  const { t } = useTranslation();
  const flow = useReactFlow<EntityNodeType, Edge>();
  const [pendingRelationSource, setPendingRelationSource] = useState<string>();

  useEffect(() => {
    if (!pendingRelationSource) return;
    const cancel = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') setPendingRelationSource(undefined);
    };
    window.addEventListener('keydown', cancel);
    return () => window.removeEventListener('keydown', cancel);
  }, [pendingRelationSource]);

  const sourceColors = useMemo(
    () => buildSourceColorMap(document.model.entities),
    [document.model.entities],
  );

  const modelNodes = useMemo<EntityNodeType[]>(
    () => document.model.entities.map((entity, index) => ({
      id: entity.id,
      type: 'entity',
      // 坐标由自动布局写入 nodePositions；万一缺了（新加的实体）就按网格兜底
      position: document.editor.nodePositions[entity.id] ?? { x: 80 + (index % 4) * 260, y: 80 + Math.floor(index / 4) * 200 },
      data: { entity, sourceColors },
      selected: selection?.kind === 'entity' && selection.id === entity.id,
    })),
    [document.model.entities, document.editor.nodePositions, selection, sourceColors],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState<EntityNodeType>(modelNodes);
  useEffect(() => setNodes(modelNodes), [modelNodes, setNodes]);

  const edges = useMemo<Edge[]>(
    () => document.model.relations.map((relation) => ({
      id: relation.id,
      source: relation.sourceEntityId,
      target: relation.targetEntityId,
      label: relation.name,
      type: 'smoothstep' as const,
      selected: selection?.kind === 'relation' && selection.id === relation.id,
      interactionWidth: 24,
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { strokeWidth: 2 },
    })),
    [document.model.relations, selection],
  );

  const createRelation = (source: string, target: string): void => {
    if (readOnly) return;
    let number = document.model.relations.length + 1;
    let name = `RELATION_${number}`;
    while (document.model.relations.some((item) => item.name.toLowerCase() === name.toLowerCase())) {
      number += 1;
      name = `RELATION_${number}`;
    }
    const id = newId();
    execute(
      { type: 'relation.add', relation: { id, name, sourceEntityId: source, targetEntityId: target } },
      { kind: 'relation', id },
    );
  };

  const connect = (connection: Connection): void => {
    if (readOnly) return;
    if (!connection.source || !connection.target) return;
    createRelation(connection.source, connection.target);
  };

  const drop = (event: React.DragEvent): void => {
    // 只读：连 preventDefault 之前的判断都提前——拖入一律不落地（新建实体/属性、连线模式）
    if (readOnly) return;
    event.preventDefault();
    const payload = event.dataTransfer.getData('application/ontology-canvas');
    if (!payload) return;
    const nodeElement = (event.target as HTMLElement).closest<HTMLElement>('.react-flow__node');
    const entityId = nodeElement?.dataset.id ?? (selection?.kind === 'entity' ? selection.id : undefined);
    if (payload === 'entity') {
      addEntity();
      return;
    }
    if (payload === 'relation') {
      if (entityId) {
        setPendingRelationSource(entityId);
        select({ kind: 'entity', id: entityId });
      }
      return;
    }
    if (payload.startsWith('attribute:')) {
      if (entityId) addAttribute(entityId, payload.slice('attribute:'.length) as AttributeType);
    }
  };

  /** 首屏视口：**不用 fitView**。
   *
   *  实测 86 个实体按 LR 铺开后，`fitView` 会把缩放压到 0.054——节点宽约 12 像素，
   *  屏幕上只剩一片灰点。全图适配对大图本来就是个错目标：那是个「让你看清有多少东西」
   *  的视图，不是「让你读内容」的视图。
   *
   *  改成固定一个可读缩放，锚在布局左上角（layoutAll 保证坐标从 origin 起算），
   *  超出部分交给用户平移缩放——这本来就是画布该有的交互。
   *
   *  依赖里绝不能放 `nodes`：它每次交互都换新引用，会与视口写入自激。 */
  const fittedRef = useRef(false);
  const ready = nodes.some((node) => node.measured?.width !== undefined);
  useEffect(() => {
    if (fittedRef.current || !ready || !document.model.entities.length) return;
    fittedRef.current = true;
    void flow.setViewport({ x: 24, y: 24, zoom: READABLE_ZOOM }, { duration: 300 });
  }, [flow, ready, document.model.entities.length]);

  /** 用户点「重新布局」后重新取景（首屏那次走上一条 effect）。
   *
   *  必须做：节点已经飞到新坐标，不重新取景的话视口还停在旧位置，
   *  屏幕上可能一个节点都看不到——按钮看起来像没反应。
   *  锚点与首屏一致（布局左上角），因为 `layoutAll` 的坐标从 ORIGIN 起算。 */
  useEffect(() => {
    if (!relayoutTick) return;
    void flow.setViewport({ x: 24, y: 24, zoom: READABLE_ZOOM }, { duration: 300 });
  }, [relayoutTick, flow]);

  return (
    <div className="canvas-wrap">
      <ReactFlow<EntityNodeType, Edge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        // 下限放到 0.05：nexonto 原本设 0.3，但它那边通常只有几十个实体；
        // 这里实测 86 个实体按 LR 铺开后，0.3 缩不进一屏——fitView 缩到下限就停，
        // 结果是打开页面看到一片空白（节点其实都渲染了，全在视口外）。
        minZoom={0.05}
        maxZoom={2}
        onNodesChange={onNodesChange}
        // 只读：拖节点、拉连线都关掉（选中/平移/缩放照旧）；各 handler 另加守卫兜底
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        onNodeClick={(_event, node) => {
          if (pendingRelationSource) {
            createRelation(pendingRelationSource, node.id);
            setPendingRelationSource(undefined);
          } else {
            select({ kind: 'entity', id: node.id });
          }
        }}
        onEdgeClick={(_event, edge) => select({ kind: 'relation', id: edge.id })}
        onPaneClick={() => {
          setPendingRelationSource(undefined);
          select(null);
        }}
        // 拖拽仍会被 React Flow 记住（本地 nodes state），但不落文档——
        // schema 里没有存坐标的地方，下次加载会按自动布局重排
        onNodeDragStop={(_event, node) => {
          if (readOnly) return;
          execute({ type: 'node.move', entityId: node.id, position: node.position });
        }}
        onConnect={connect}
        onDragOver={(event) => {
          // 不 preventDefault = 浏览器判「不可放置」，光标直接显示禁用态
          if (readOnly) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = 'copy';
        }}
        onDrop={drop}
        proOptions={{ hideAttribution: true }}
        deleteKeyCode={null}
      >
        <Controls showInteractive={false} />
      </ReactFlow>
      {!document.model.entities.length ? <div className="canvas-empty">{t('editor.emptyCanvas')}</div> : null}
      {pendingRelationSource ? <div className="relation-mode-hint">{t('relation.pickTarget')}</div> : null}
      <div className="canvas-hint">{t('editor.zoomHint')}</div>
    </div>
  );
}

export function ModelCanvas(props: CanvasProps): ReactNode {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
