/**
 * 多源视图（只读）：知识库下各数据源 ds 级编译 schema 的合并画布。
 *
 * 布局按源分区并排；实体打来源徽章、id 命名空间化（不同源同名不并）；
 * 「概念叠层」把抽象 schema 的跨源落点连成紫色虚线，点概念高亮其全部落点、
 * 其余压暗。只读——编辑走数据源级画布（`OntologyCanvasPage`）与知识库级抽象编辑器
 * （`AbstractSchemaEditorPage`）。
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Alert, Button, Checkbox, Empty, Spin, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { Controls, MarkerType, ReactFlow, ReactFlowProvider, ViewportPortal, type Edge } from '@xyflow/react';
import { useTranslation } from 'react-i18next';

import { layoutRegions, orderByDegree } from '../layout.js';
import { MultiSourceEntityNode, type MultiSourceNodeType } from './MultiSourceEntityNode.js';
import { buildMultiSourceModel } from './model.js';
import { loadMultiSourceData, type MultiSourceData } from './load.js';
import { sourceColor } from './palette.js';
import './styles.scss';

/** 首屏缩放：与单源画布一致（节点宽 220px，0.75 下约 165px，正文可读）。 */
const READABLE_ZOOM = 0.75;

const nodeTypes = { msEntity: MultiSourceEntityNode };

export function MultiSourceView({ kbId }: { kbId: string }): ReactNode {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const [data, setData] = useState<MultiSourceData>();
  /** 源过滤：存**关掉**的源 id（缺省全开） */
  const [disabledSources, setDisabledSources] = useState<Set<string>>(new Set());
  /** 选中的概念（高亮其全部落点）；undefined = 无 */
  const [selectedConcept, setSelectedConcept] = useState<string>();
  const [crossOnly, setCrossOnly] = useState(false);
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(undefined);
    // 换库/刷新时把视图态归零——否则上一份的选中概念、关掉的源会串到新数据上
    setSelectedConcept(undefined);
    setCrossOnly(false);
    setDisabledSources(new Set());
    loadMultiSourceData(kbId)
      .then((result) => { if (!cancelled) setData(result); })
      .catch((e: unknown) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [kbId, reloadTick]);

  const model = useMemo(
    () => (data ? buildMultiSourceModel(data.sources, data.concepts) : undefined),
    [data],
  );

  /** 布局：每个源一组，组内按连接度排（hub 在前）；空源也给区域框 */
  const layout = useMemo(() => {
    if (!model) return undefined;
    const groups = model.sources.map((source) => {
      const own = model.entities.filter((entity) => entity.sourceId === source.id);
      const ordered = orderByDegree(
        own,
        model.relations.filter((relation) => relation.sourceId === source.id),
      );
      return { sourceId: source.id, entityIds: ordered.map((entity) => entity.id) };
    });
    return layoutRegions(groups);
  }, [model]);

  /** 跨源概念面板行：有跨源连线的排前面，只剩失效落点的殿后 */
  const conceptRows = useMemo(() => {
    if (!data || !model) return [];
    const linksByConcept = new Map<string, number>();
    for (const link of model.conceptLinks) {
      linksByConcept.set(link.concept, (linksByConcept.get(link.concept) ?? 0) + 1);
    }
    return data.concepts
      .map((concept) => ({
        name: concept.name,
        linkCount: linksByConcept.get(concept.name) ?? 0,
        staleCount: concept.landings.filter((landing) => landing.stale).length,
      }))
      .filter((row) => row.linkCount > 0 || row.staleCount > 0)
      .sort((left, right) => right.linkCount - left.linkCount);
  }, [data, model]);

  const visibleEntityIds = useMemo(() => {
    if (!model) return new Set<string>();
    const cross = new Set(model.crossSourceEntityIds);
    return new Set(
      model.entities
        .filter((entity) => !disabledSources.has(entity.sourceId))
        .filter((entity) => !crossOnly || cross.has(entity.id))
        .map((entity) => entity.id),
    );
  }, [model, disabledSources, crossOnly]);

  const nodes = useMemo<MultiSourceNodeType[]>(() => {
    if (!model || !layout) return [];
    return model.entities
      .filter((entity) => visibleEntityIds.has(entity.id))
      .map((entity) => {
        const highlighted = selectedConcept !== undefined && entity.concepts.includes(selectedConcept);
        const dimmed = selectedConcept !== undefined && !highlighted;
        return {
          id: entity.id,
          type: 'msEntity' as const,
          position: layout.positions[entity.id] ?? { x: 0, y: 0 },
          data: { entity, color: sourceColor(entity.sourceIndex) },
          draggable: false,
          className: [highlighted ? 'ms-node-hl' : '', dimmed ? 'ms-node-dim' : ''].filter(Boolean).join(' '),
        };
      });
  }, [model, layout, visibleEntityIds, selectedConcept]);

  const edges = useMemo<Edge[]>(() => {
    if (!model) return [];
    const relationEdges: Edge[] = model.relations
      .filter((relation) => visibleEntityIds.has(relation.sourceEntityId) && visibleEntityIds.has(relation.targetEntityId))
      .map((relation) => {
        const index = model.sources.findIndex((source) => source.id === relation.sourceId);
        return {
          id: relation.id,
          source: relation.sourceEntityId,
          target: relation.targetEntityId,
          type: 'smoothstep',
          interactionWidth: 0,
          markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12 },
          // 选中概念时源内关系退成背景（否则 240 个节点的网会盖住概念线）
          style: { stroke: sourceColor(index), strokeWidth: 1.4, opacity: selectedConcept !== undefined ? 0.08 : 0.4 },
        };
      });

    const linkEdges: Edge[] = model.conceptLinks
      .filter((link) => visibleEntityIds.has(link.sourceEntityId) && visibleEntityIds.has(link.targetEntityId))
      .map((link) => {
        const active = selectedConcept === link.concept;
        return {
          id: link.id,
          source: link.sourceEntityId,
          target: link.targetEntityId,
          type: 'smoothstep',
          label: link.concept,
          animated: active,
          zIndex: 5,
          markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12, color: '#9333ea' },
          labelStyle: { fontSize: 11, fill: '#7c3aed' },
          style: {
            stroke: '#9333ea',
            strokeWidth: 2.2,
            strokeDasharray: '6 4',
            opacity: selectedConcept !== undefined && !active ? 0.08 : 1,
          },
        };
      });

    return [...relationEdges, ...linkEdges];
  }, [model, visibleEntityIds, selectedConcept]);

  if (loading) return <div className="ontology-canvas-loading"><Spin /></div>;

  if (error) {
    return (
      <Alert
        type="error" showIcon
        message={t('multiSource.loadFailed')}
        description={error}
        action={<Button size="small" onClick={() => setReloadTick((tick) => tick + 1)}>{t('multiSource.reload')}</Button>}
      />
    );
  }

  if (!data || !model || !layout) return null;

  if (!data.sources.length) return <Empty description={t('multiSource.empty')} />;
  if (!model.entities.length) return <Empty description={t('multiSource.noEntities')} />;

  return (
    <div className="ms-view">
      <aside className="ms-side">
        <div className="ms-side-block">
          <h4>{t('multiSource.sourcesTitle')}</h4>
          {model.sources.map((source, index) => (
            <label key={source.id} className="ms-source-row">
              <Checkbox
                checked={!disabledSources.has(source.id)}
                onChange={(event) => {
                  setDisabledSources((prev) => {
                    const next = new Set(prev);
                    if (event.target.checked) next.delete(source.id);
                    else next.add(source.id);
                    return next;
                  });
                }}
              />
              <span className="ms-source-dot" style={{ background: sourceColor(index) }} />
              <span className="ms-source-name">{source.name}</span>
              <span className="ms-source-count">
                {source.hasSchema ? t('multiSource.entityCount', { n: source.entityCount }) : t('multiSource.noSchema')}
              </span>
            </label>
          ))}
        </div>

        <div className="ms-side-block">
          <h4>{t('multiSource.conceptsTitle')}</h4>
          {conceptRows.length === 0 ? (
            <p className="ms-side-empty">{t('multiSource.conceptsEmpty')}</p>
          ) : conceptRows.map((row) => (
            <button
              key={row.name}
              type="button"
              className={`ms-concept-row ${selectedConcept === row.name ? 'is-selected' : ''}`}
              onClick={() => setSelectedConcept((prev) => (prev === row.name ? undefined : row.name))}
            >
              <span className="ms-concept-name">{row.name}</span>
              {row.linkCount > 0 ? <span className="ms-concept-links">{t('multiSource.conceptLinks', { n: row.linkCount })}</span> : null}
              {row.staleCount > 0 ? <span className="ms-concept-stale">{t('multiSource.conceptStale', { n: row.staleCount })}</span> : null}
            </button>
          ))}
        </div>

        <div className="ms-side-block">
          <Checkbox checked={crossOnly} onChange={(event) => setCrossOnly(event.target.checked)}>
            {t('multiSource.crossOnly')}
          </Checkbox>
        </div>
      </aside>

      <div className="ms-canvas">
        <div className="ms-toolbar">
          <Tag>{t('multiSource.readOnly')}</Tag>
          <span className="ms-toolbar-counts">
            {t('multiSource.counts', { sources: model.sources.length, entities: model.entities.length, links: model.conceptLinks.length })}
          </span>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => setReloadTick((tick) => tick + 1)}>
            {t('common.refresh')}
          </Button>
        </div>
        <div className="ms-canvas-wrap">
          <ReactFlowProvider>
            <ReactFlow<MultiSourceNodeType, Edge>
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              minZoom={0.05}
              maxZoom={2}
              onlyRenderVisibleElements
              defaultViewport={{ x: 24, y: 24, zoom: READABLE_ZOOM }}
              proOptions={{ hideAttribution: true }}
              deleteKeyCode={null}
            >
              <ViewportPortal>
                {layout.regions.map((region) => {
                  const source = model.sources.find((item) => item.id === region.sourceId);
                  const index = model.sources.findIndex((item) => item.id === region.sourceId);
                  return (
                    <div
                      key={region.sourceId}
                      className="ms-region"
                      style={{
                        transform: `translate(${region.x}px, ${region.y}px)`,
                        width: region.width,
                        height: region.height,
                        borderColor: `${sourceColor(index)}55`,
                      }}
                    >
                      <span className="ms-region-label" style={{ color: sourceColor(index) }}>
                        {source?.name}
                        {source && !source.hasSchema ? ` · ${t('multiSource.noSchema')}` : ''}
                      </span>
                    </div>
                  );
                })}
              </ViewportPortal>
              <Controls showInteractive={false} />
            </ReactFlow>
          </ReactFlowProvider>
          <div className="ms-hint">{t('multiSource.hint')}</div>
        </div>
      </div>
    </div>
  );
}
