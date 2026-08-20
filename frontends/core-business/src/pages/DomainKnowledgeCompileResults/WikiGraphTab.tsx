// [jonex] OpenKB Wiki 图谱只读视图 — 用 @antv/g6 渲染，不复用 KnowledgeGraphPanel
// 不复用的原因：KnowledgeGraphPanel 深度耦合本体接口（getOntologyGraph/expandOntologyNeighbors/
// getOntologyStatistics），Wiki 侧只有 getWikiGraph，强行注入 loader 会产生半功能不可用分支。
import { useState, useEffect, useRef } from 'react';
import { Empty, Spin, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { getWikiGraph } from '@/api/domainKnowledge';
import type { WikiGraphData } from '@/types/domainKnowledge';

interface WikiGraphTabProps {
  kbId: string;
  /** [jonex] 文档级子图必传（透传 parse-results/graph 的 document_id） */
  docId: string;
}

export default function WikiGraphTab({ kbId, docId }: WikiGraphTabProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<any>(null);
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<WikiGraphData | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getWikiGraph(kbId, 200, docId)
      .then((res) => {
        if (cancelled) return;
        setData(res);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        message.error(t('common.loadFailed'));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kbId, docId, t]);

  // Render graph with @antv/g6
  useEffect(() => {
    if (!data || !containerRef.current) return;

    let cancelled = false;
    let g6Graph: any = null;

    import('@antv/g6')
      .then(({ Graph: G6Graph }) => {
        if (cancelled || !containerRef.current) return;

        // Clean up previous graph instance
        if (graphRef.current) {
          graphRef.current.destroy();
          graphRef.current = null;
        }

        const { nodes, edges } = data;
        if (!nodes.length) return;

        // Assign colors by type
        const typeColors: Record<string, string> = {};
        const palette = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#f97316'];
        let colorIdx = 0;
        nodes.forEach((n) => {
          if (!typeColors[n.type]) {
            typeColors[n.type] = palette[colorIdx % palette.length];
            colorIdx++;
          }
        });

        const g6Nodes = nodes.map((n) => ({
          id: n.id,
          label: n.name,
          style: { fill: typeColors[n.type] || '#3b82f6' },
          data: { description: n.description, type: n.type },
        }));

        const g6Edges = edges.map((e) => ({
          source: e.source,
          target: e.target,
        }));

        g6Graph = new G6Graph({
          container: containerRef.current,
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
          layout: {
            type: 'force',
            preventOverlap: true,
            linkDistance: 120,
            nodeStrength: -100,
            edgeStrength: 0.1,
          },
          behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
          node: {
            style: {
              size: 28,
              labelText: (d: any) => d.label,
              labelFontSize: 11,
              labelFill: '#1e293b',
              labelPlacement: 'bottom',
              labelOffsetY: 6,
            },
            state: {
              hover: {
                size: 34,
              },
            },
          },
          edge: {
            style: {
              stroke: '#cbd5e1',
              lineWidth: 1,
              endArrow: true,
            },
          },
          autoFit: 'view',
          animation: false,
        });

        g6Graph.setData({ nodes: g6Nodes, edges: g6Edges });
        g6Graph.render();

        // Tooltip on hover
        g6Graph.on('node:pointerenter', (evt: any) => {
          const nodeData = evt?.target?.getData?.() || evt?.data || evt?.target?.attributes;
          if (nodeData?.data?.description) {
            g6Graph.setCursor('pointer');
          }
        });

        graphRef.current = g6Graph;
      })
      .catch(() => {
        if (!cancelled) message.error(t('common.loadFailed'));
      });

    return () => {
      cancelled = true;
      if (g6Graph) {
        try {
          g6Graph.destroy();
        } catch {
          // ignore cleanup errors
        }
      }
    };
  }, [data, t]);

  // Handle resize
  useEffect(() => {
    const handleResize = () => {
      if (graphRef.current && containerRef.current) {
        try {
          graphRef.current.setSize(containerRef.current.clientWidth, containerRef.current.clientHeight);
        } catch {
          // ignore
        }
      }
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: 'calc(100vh - 290px)' }}>
        <Spin />
      </div>
    );
  }

  if (!data || !data.nodes.length) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: 'calc(100vh - 290px)' }}>
        <Empty description={t('compile.emptyWikiGraph')} />
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      style={{
        width: '100%',
        height: 'calc(100vh - 290px)',
        minHeight: 500,
        background: '#fafbfc',
        borderTop: '1px solid #f1f5f9',
      }}
    />
  );
}
