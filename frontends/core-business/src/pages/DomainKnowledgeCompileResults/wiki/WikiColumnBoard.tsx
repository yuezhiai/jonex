/** Wiki graph data with the ontology chain's shared Miller-column presentation. */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Drawer, Select, Spin, Tag, Image } from 'antd';
import { useDocumentViewer, inferMediaType } from '@/components/DocumentViewer';
import MarkdownContent from '@/components/MarkdownContent';
import {
  getWikiImageUrl, getWikiGraphTypes, searchWikiGraphNodes, getWikiGraphNeighbors, getWikiGraphEvidence, getWikiGraphNode,
  type WikiGraphNode, type WikiGraphEdge, type WikiGraphEvidence, type WikiGraphFamily,
  type WikiGraphTypes, type WikiGraphPage,
} from '@/api/wikiGraph';
import { ColumnEmpty, ColumnError, ColumnLoading, ColumnShell, ColumnStage, NodeCard,
  useColumnConnectors, type ColumnItem } from '../columnBoard/parts';

const PAGE_SIZE = 20;
type Column = { key: string } & ({ kind: 'types' } | { kind: 'nodes'; nodeType: string } | { kind: 'neighbors'; node: WikiGraphNode });
interface Props {
  kbId: string; documentId?: string; height?: string | number;
}
const typeKey = (kind: string) => `domainKnowledge.wikiGraph.nodeTypes.${kind}`;
const familyKey = (family: string) => `domainKnowledge.wikiGraph.families.${family}`;
function conditionText(value: WikiGraphEdge['qualifiers']) {
  const records = Array.isArray(value) ? value : [value];
  return [...new Set(records.flatMap(record => Object.entries(record || {}).filter(([key]) => !["catalog_relation_id", "support_kind", "binding_id", "association_method"].includes(key)).map(([, value]) => value).filter(item => typeof item === 'string') as string[]))].join('；');
}

export default function WikiColumnBoard(props: Props) {
  return <GraphBoard key={`${props.kbId}:${props.documentId || ''}`} {...props} />;
}
function GraphBoard({ kbId, documentId = '', height = '100%' }: Props) {
  const { t } = useTranslation();
  const sequence = useRef(1);
  const [columns, setColumns] = useState<Column[]>([{ key: 'col-0', kind: 'types' }]);
  const [types, setTypes] = useState<WikiGraphTypes | null>(null);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [detail, setDetail] = useState<{ node: WikiGraphNode; edge?: WikiGraphEdge } | null>(null);
  const connectors = useColumnConnectors(columns.length);
  useEffect(() => {
    let alive = true;
    setTypes(null); setError('');
    getWikiGraphTypes(kbId, { document_id: documentId }).then(result => {
      if (alive) setTypes(result);
    }).catch(err => { if (alive) setError(err?.message || t('common.loadFailed')); });
    return () => { alive = false; };
  }, [kbId, documentId, reload, t]);
  const drill = useCallback((index: number, node: WikiGraphNode) => {
    setColumns(previous => {
      const prefix = previous.slice(0, index + 1);
      const existing = prefix.findIndex(c => c.kind === 'neighbors' && c.node.node_id === node.node_id);
      if (existing >= 0) return prefix.slice(0, existing + 1);
      return [...prefix, { key: `col-${sequence.current++}`, kind: 'neighbors', node }];
    });
  }, []);
  if (error) return <Alert type="error" showIcon message={t('common.loadFailed')} description={error}
    action={<Button onClick={() => setReload(v => v + 1)}>{t('common.retry')}</Button>} />;
  if (!types) return <div style={{ padding: 48, textAlign: 'center' }}><Spin /></div>;
  return <>
    <ColumnStage connectors={connectors} height={height}>
      {columns.map((column, index) => column.kind === 'types' ? <TypesColumn key={column.key} types={types}
        onChanged={connectors.scheduleConnectors}
        onChoose={nodeType => setColumns(previous => [previous[0], { key: `col-${sequence.current++}`, kind: 'nodes', nodeType }])} /> :
        <DataColumn key={column.key} kbId={kbId} documentId={documentId} revision={types.graph_revision} column={column}
          visited={new Set(columns.slice(0, index).flatMap(c => c.kind === 'neighbors' ? [c.node.node_id] : []))}
          onChanged={connectors.scheduleConnectors} onChoose={node => drill(index, node)}
          onRefresh={() => { setColumns([{ key: 'col-0', kind: 'types' }]); setDetail(null); setReload(v => v + 1); }}
          onDetail={(node, edge) => setDetail({ node, edge })} />)}
    </ColumnStage>
    {detail && <NodeDetail kbId={kbId} documentId={documentId} revision={types.graph_revision} detail={detail}
      onClose={() => setDetail(null)} />}
  </>;
}
function TypesColumn({ types, onChoose, onChanged }: { types: WikiGraphTypes; onChoose: (kind: string) => void; onChanged: () => void }) {
  const { t } = useTranslation();
  const [keyword, setKeyword] = useState('');
  const [selected, setSelected] = useState('');
  const items = types.items.filter(item => t(typeKey(item.node_type)).toLowerCase().includes(keyword.toLowerCase()) || item.node_type.includes(keyword));
  useEffect(() => { onChanged(); }, [keyword, selected, types, onChanged]);
  return <ColumnShell kicker={t('domainKnowledge.wikiGraph.kicker')} title={t('domainKnowledge.wikiGraph.types')}
    count={items.length} searchable search={keyword} onSearch={setKeyword}>
    {items.length ? items.map(item => <NodeCard key={item.node_type} selected={selected === item.node_type}
      item={{ key: item.node_type, label: t(typeKey(item.node_type)), sub: t('domainKnowledge.wikiGraph.nodeCount', { count: item.count }), isType: true }}
      onClick={() => { setSelected(item.node_type); onChoose(item.node_type); }} />) : <ColumnEmpty text={t('domainKnowledge.wikiGraph.empty')} />}
  </ColumnShell>;
}
function DataColumn({ kbId, documentId, revision, column, visited, onChoose, onDetail, onChanged, onRefresh }: {
  kbId: string; documentId: string; revision: string; column: Exclude<Column, { kind: 'types' }>;
  visited: Set<string>; onChoose: (node: WikiGraphNode) => void;
  onDetail: (node: WikiGraphNode, edge?: WikiGraphEdge) => void; onChanged: () => void; onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const [keyword, setKeyword] = useState('');
  const [family, setFamily] = useState<WikiGraphFamily | 'all'>('all');
  const [items, setItems] = useState<(WikiGraphNode | WikiGraphEdge)[]>([]);
  const [total, setTotal] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const [reload, setReload] = useState(0);
  const requestSequence = useRef(0);
  const load = useCallback(async (nextCursor = '') => {
    const token = ++requestSequence.current;
    setLoading(true); setError('');
    try {
      const options = { graph_revision: revision, document_id: documentId, cursor: nextCursor, limit: PAGE_SIZE };
      const result: WikiGraphPage<WikiGraphNode | WikiGraphEdge> = column.kind === 'nodes'
        ? await searchWikiGraphNodes(kbId, { ...options, query: keyword, node_type: column.nodeType })
        : await getWikiGraphNeighbors(kbId, { ...options, node_id: column.node.node_id, families: family === 'all' ? undefined : [family] });
      if (token !== requestSequence.current) return;
      setItems(previous => nextCursor ? [...previous, ...result.items] : result.items);
      setTotal(result.total); setCursor(result.next_cursor);
    } catch (err) { if (token === requestSequence.current) setError((err as Error)?.message || t('common.loadFailed')); }
    finally { if (token === requestSequence.current) setLoading(false); }
  }, [kbId, documentId, revision, column, keyword, family, t]);
  useEffect(() => {
    setItems([]); setCursor(null); setTotal(0); setLoading(true); setSelected('');
    const timer = setTimeout(() => { void load(); }, keyword ? 200 : 0);
    return () => { clearTimeout(timer); ++requestSequence.current; };
  }, [load, reload, keyword]);
  useEffect(() => { onChanged(); }, [items, selected, loading, onChanged]);
  const isNodes = column.kind === 'nodes';
  return <ColumnShell kicker={isNodes ? t(typeKey(column.nodeType)) : column.node.label}
    title={t(isNodes ? 'domainKnowledge.wikiGraph.nodes' : 'domainKnowledge.wikiGraph.neighbors')}
    count={total} searchable={isNodes} search={keyword} onSearch={setKeyword}
    headerExtra={isNodes ? undefined : <Select size="small" style={{ width: '100%' }} value={family}
      aria-label={t('domainKnowledge.wikiGraph.relationFilter')} onChange={setFamily}
      options={['all', 'semantic', 'navigation', 'provenance'].map(value => ({ value, label: t(familyKey(value)) }))} />}>
    {items.map(item => {
      const edge = 'edge_id' in item ? item : undefined;
      const node = edge ? edge.node : item as WikiGraphNode;
      const key = edge?.edge_id || node.node_id;
      const scopeName = ['company', 'organization', 'sheet'].map(field => node.scope?.[field]).find(value => typeof value === 'string');
      const card: ColumnItem = { key, label: node.label, tag: t(typeKey(node.node_type)),
        sub: edge ? [t(familyKey(edge.family)), t('domainKnowledge.wikiGraph.evidence', { count: edge.evidence_count }),
          edge.conflict ? t('domainKnowledge.wikiGraph.conflict') : '',
          visited.has(node.node_id) ? t('domainKnowledge.wikiGraph.visited') : ''].filter(Boolean).join(' · ')
          : node.node_type === 'claim' ? node.support_status : (scopeName as string | undefined) || node.category,
        desc: edge ? conditionText(edge.qualifiers) || edge.description || node.description : node.description,
        relation: edge ? `${edge.direction === 'in' ? '←' : '→'} ${edge.predicate}` : undefined };
      return <NodeCard key={key} item={card} selected={selected === key} grayed={visited.has(node.node_id)}
          onClick={() => { setSelected(key); onChoose(node); }} onOpen={() => onDetail(node, edge)}
          openHint={t('domainKnowledge.wikiGraph.detailHint')} />;
    })}
    {loading && <ColumnLoading />}
    {error && <ColumnError message={error} onRetry={() => setReload(v => v + 1)} />}
    {error && <Button size="small" onClick={onRefresh}>{t('domainKnowledge.wikiGraph.refresh')}</Button>}
    {!loading && !error && !items.length && <ColumnEmpty text={t('domainKnowledge.wikiGraph.empty')} />}
    {!loading && !error && cursor && <Button onClick={() => void load(cursor)}>{t('domainKnowledge.wikiGraph.loadMore', { count: items.length, total })}</Button>}
  </ColumnShell>;
}
function NodeDetail({ kbId, documentId, revision, detail, onClose }: {
  kbId: string; documentId: string; revision: string; detail: { node: WikiGraphNode; edge?: WikiGraphEdge };
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { openDocument, viewer } = useDocumentViewer();
  const [imageUrl, setImageUrl] = useState('');
  const [content, setContent] = useState<string | null>(null);
  const [contentError, setContentError] = useState('');
  const [contentLoading, setContentLoading] = useState(false);
  const [items, setItems] = useState<WikiGraphEvidence[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const contentSequence = useRef(0);
  const load = useCallback(async (next = '') => {
    const token = ++sequence.current;
    setLoading(true); setError('');
    try {
      const result = await getWikiGraphEvidence(kbId, { graph_revision: revision, document_id: documentId,
        ...(detail.edge ? { edge_id: detail.edge.edge_id } : { node_id: detail.node.node_id }), cursor: next });
      if (token === sequence.current) { setItems(previous => next ? [...previous, ...result.items] : result.items); setCursor(result.next_cursor); }
    } catch (err) { if (token === sequence.current) setError((err as Error)?.message || t('common.loadFailed')); }
    finally { if (token === sequence.current) setLoading(false); }
  }, [kbId, documentId, revision, detail, t]);
  useEffect(() => {
    setItems([]); setContent(null); setContentError(''); setContentLoading(false); void load();
    return () => { ++sequence.current; ++contentSequence.current; };
  }, [load]);
  useEffect(() => {
    let alive = true;
    setImageUrl('');
    if (detail.node.asset_id && detail.node.document_id) {
      getWikiImageUrl(kbId, detail.node.document_id, revision, detail.node.asset_id)
        .then(url => { if (alive) setImageUrl(url); })
        .catch(err => { if (alive) setContentError((err as Error).message); });
    }
    return () => { alive = false; };
  }, [kbId, revision, detail.node]);
  const openSource = (id: string, name: string, mime = '', page?: number) => {
    openDocument({ docId: id, fileName: name, mediaType: inferMediaType(name), page });
  };
  const openPage = async () => {
    const token = ++contentSequence.current;
    setContentLoading(true); setContentError('');
    try { const result = await getWikiGraphNode(kbId, detail.node.node_id, revision, documentId, true); if (token === contentSequence.current) setContent(result.node.content || ''); }
    catch (err) { if (token === contentSequence.current) setContentError((err as Error)?.message || t('common.loadFailed')); }
    finally { if (token === contentSequence.current) setContentLoading(false); }
  };
  return <Drawer open title={detail.node.label} onClose={onClose} size="large">
    <Tag>{t(typeKey(detail.node.node_type))}</Tag>
    <div style={{ margin: '12px 0', color: '#64748b' }}>{t('domainKnowledge.wikiGraph.version')} {revision.slice(0, 12)}</div>
    {imageUrl && <Image src={imageUrl} alt={detail.node.label} style={{ maxHeight: 520, objectFit: 'contain' }} />}
    {detail.node.description && <MarkdownContent content={detail.node.description} />}
    {detail.edge && <>
      <p>{detail.edge.direction === 'in' ? '←' : '→'} {detail.edge.predicate} · {t(familyKey(detail.edge.family))}</p>
      {detail.edge.conflict && <Tag color="error">{t('domainKnowledge.wikiGraph.conflict')}</Tag>}
      {conditionText(detail.edge.qualifiers) && <p>{conditionText(detail.edge.qualifiers)}</p>}
    </>}
    {detail.node.page_path && <Button loading={contentLoading} onClick={() => void openPage()}>{t('domainKnowledge.wikiGraph.openPage')}</Button>}
    {detail.node.document_id && detail.node.node_type === 'document' && <Button onClick={() => openSource(detail.node.document_id!, detail.node.label, detail.node.mime_type)}>{t('domainKnowledge.wikiGraph.openSource')}</Button>}
    <h3>{t('domainKnowledge.wikiGraph.sourceEvidence')}</h3>
    {items.map(item => <section key={item.evidence_id} style={{ borderTop: '1px solid #e2e8f0', padding: '16px 0' }}>
      <strong>{item.file_name || item.document_id || item.passage_id}</strong> <Tag>{item.support_status}</Tag>
      <blockquote style={{ whiteSpace: 'pre-wrap', margin: '12px 0', paddingLeft: 12, borderLeft: '3px solid #4b84eb' }}>{item.excerpt}</blockquote>
      <div>{item.chunk_id}{item.start_char != null ? ` · ${item.start_char}–${item.end_char}` : ''}</div>
      {item.source_sha256 && <div style={{ overflowWrap: 'anywhere' }}>{t('domainKnowledge.wikiGraph.sourceVersion')} <code>{item.source_sha256}</code></div>}
      {item.document_id && <Button type="link" onClick={() => openSource(item.document_id, item.file_name || item.document_id, item.mime_type, item.pdf_page)}>{t('domainKnowledge.wikiGraph.openSource')}</Button>}
    </section>)}
    {loading && <Spin />}
    {error && <Alert type="error" message={error} action={<Button onClick={() => void load()}>{t('common.retry')}</Button>} />}
    {!loading && !error && !items.length && <p>{t('domainKnowledge.wikiGraph.noEvidence')}</p>}
    {!loading && !error && cursor && <Button onClick={() => void load(cursor)}>{t('domainKnowledge.wikiGraph.moreEvidence')}</Button>}
    {contentError && <Alert type="error" message={contentError} />}
    {content !== null && <MarkdownContent content={content} />}
    {viewer}
  </Drawer>;
}
