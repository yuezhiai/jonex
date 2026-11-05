import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Input, Modal, Select, Space, Spin, Switch, Tag, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { listDocumentVersions, getDocumentVersionUrl } from '@/api/domainKnowledge';
import {
  listWikiScenarios, configureWikiScenario, runWikiScenario, readWikiScenario, reviewWikiScenario,
  listWikiScenarioMaterials, readWikiScenarioMaterial, annotateWikiScenario, type ScenarioMaterial,
  type ScenarioStrategy, type WikiScenario, type WikiScenarioState, type ScenarioResources,
} from '@/api/wikiScenarios';

export default function WikiScenarioPanel({ kbId, canWrite = false }: { kbId: string; canWrite?: boolean }) {
  const { t } = useTranslation();
  const [state, setState] = useState<WikiScenarioState>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const [resources, setResources] = useState<ScenarioResources>();
  const [offset, setOffset] = useState(0);
  const [preview, setPreview] = useState<{ url: string; text: string; image: boolean; bbox?: number[] }>();
  const [annotation, setAnnotation] = useState<{ scene: WikiScenario; version: number; units: ScenarioMaterial[]; total: number;
    offset: number; unit?: ScenarioMaterial; label: string; kind: string; quote: string; interpretation: string }>();
  const sourceSelection = useRef<HTMLTextAreaElement>(null);
  const resourceRequest = useRef(0);
  const currentKb = useRef(kbId); currentKb.current = kbId;
  const [documents, setDocuments] = useState<{ value: string; label: string }[]>([]);
  const [drafts, setDrafts] = useState<Record<string, { strategy: ScenarioStrategy; documents: string[] }>>({});
  const load = useCallback(async () => {
    const value = await listWikiScenarios(kbId);
    if (currentKb.current === kbId) { setState(value); setDocuments(value.documents.map(d => ({ value: d.document_id, label: d.label }))); }
    return value;
  }, [kbId]);
  useEffect(() => {
    resourceRequest.current += 1;
    setState(undefined); setResources(undefined); setSelected(''); setDrafts({}); setOffset(0); setPreview(undefined); setAnnotation(undefined);
    setBusy(true); setError('');
    load().catch(e => setError(e.message)).finally(() => setBusy(false));

  }, [kbId, load, t]);
  useEffect(() => {
    if (!state?.items.some(s => s.status === 'running' || s.status === 'queued')) return;
    const timer = window.setInterval(() => { void load().catch(e => setError(e.message)); }, 2000);
    return () => window.clearInterval(timer);
  }, [state, load]);
  const draft = (item: WikiScenario) => drafts[item.id] || { strategy: item.strategy || item.strategies[0] || 'on_demand', documents: item.document_ids || [] };
  async function configure(item: WikiScenario, enabled: boolean) {
    setBusy(true); setError('');
    try {
      const latest = await load(); const value = draft(item);
      setState(await configureWikiScenario(kbId, item.id, { expected_version: latest.version, enabled,
        strategy: value.strategy, document_ids: value.documents }));
      if (selected === item.id) { resourceRequest.current += 1; setSelected(''); setResources(undefined); setOffset(0); }
      message.success(t('wikiScenarios.saved'));
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function read(id: string, nextOffset = 0) {
    const sequence = ++resourceRequest.current;
    const fixed = nextOffset > 0 && resources ? { revision: resources.revision, review_version: resources.review_version } : {};
    setSelected(id); setError(''); setBusy(true);
    try { const value = await readWikiScenario(kbId, id, { offset: nextOffset, limit: 50, ...fixed });
      if (sequence === resourceRequest.current) { setResources(value); setOffset(nextOffset); }
    } catch (e) { if (sequence === resourceRequest.current) { setError((e as Error).message); setResources(undefined); } }
    finally { if (sequence === resourceRequest.current) setBusy(false); }
  }
  async function original(fragment: ScenarioResources['items'][number]['fragments'][number]) {
    try {
      const versions = await listDocumentVersions(fragment.document_id);
      const version = versions.items.find(v => v.contentHash === fragment.source_sha256);
      if (!version) throw new Error(t('wikiScenarios.originalVersionMissing'));
      const ticket = await getDocumentVersionUrl(fragment.document_id, version.versionNo);
      const originalImage = /\.(png|jpe?g|webp)$/i.test(version.fileName);
      const box = fragment.locator.image_bbox;
      const originalUrl = !originalImage && fragment.locator.kind === 'pdf_page' && Number.isInteger(fragment.locator.page)
        ? ticket.url + '#page=' + fragment.locator.page : ticket.url;
      setPreview({ url: originalUrl, text: fragment.text || t('wikiScenarios.visualCandidate'), image: originalImage,
        bbox: originalImage && fragment.locator.kind === 'original_image' && Array.isArray(box) ? box as number[] : undefined });
    }
    catch (e) { setError((e as Error).message); }
  }
  async function manual(item: WikiScenario, nextOffset = 0) {
    try {
      const [latest, units] = await Promise.all([load(), listWikiScenarioMaterials(kbId, item.id, nextOffset)]);
      setAnnotation({ scene: item, version: latest.version, units: units.items, total: units.total, offset: nextOffset,
        label: '', kind: item.kinds[0], quote: '', interpretation: '' });
    } catch (e) { setError((e as Error).message); }
  }
  async function annotationUnit(id: string) {
    if (!annotation) return;
    try { const value = await readWikiScenarioMaterial(kbId, annotation.scene.id, id);
      setAnnotation(a => a ? { ...a, unit: value.unit, quote: '' } : a); }
    catch (e) { setError((e as Error).message); }
  }
  async function saveAnnotation() {
    if (!annotation?.unit || !annotation.label.trim()) { message.info(t('wikiScenarios.annotationRequired')); return; }
    try { await annotateWikiScenario(kbId, annotation.scene.id, { expected_version: annotation.version,
      kind: annotation.kind, label: annotation.label, interpretation: annotation.interpretation,
      fragments: [{ unit_id: annotation.unit.unit_id, quote: annotation.unit.modality === 'image' ? '' : annotation.quote }] });
      const id = annotation.scene.id; setAnnotation(undefined); await load(); await read(id);
    } catch (e) { setError((e as Error).message); }
  }
  return <div style={{ padding: 24, maxWidth: 1100 }}>
    <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }}>
      <Typography.Title level={4} style={{ margin: 0 }}>{t('wikiScenarios.title')}</Typography.Title>
      <Button onClick={() => void load().catch(e => setError(e.message))}>{t('common.refresh', { defaultValue: '刷新' })}</Button>
    </Space>
    <Typography.Paragraph type="secondary">{t('wikiScenarios.description')}</Typography.Paragraph>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
    <Spin spinning={busy}>
      {state?.items.map(item => {
        const value = draft(item);
        return <div key={item.id} data-scenario-id={item.id} style={{ padding: '22px 0', borderTop: '1px solid var(--ant-color-border-secondary, #e7ebf0)' }}>
          <Space wrap>
            <Typography.Text strong>{item.label}</Typography.Text>
            <Tag>{t('wikiScenarios.status.' + item.status, { defaultValue: item.status })}</Tag>
            <Switch checked={Boolean(item.enabled)} disabled={!canWrite || busy}
              aria-label={item.label} onChange={enabled => void configure(item, enabled)} />
          </Space>
          <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>{item.description}</Typography.Paragraph>
          <Space wrap>
            <Select value={value.strategy} disabled={!canWrite} style={{ width: 150 }}
              aria-label={t('wikiScenarios.strategy')}
              options={item.strategies.map(key => ({ value: key, label: t('wikiScenarios.strategies.' + key, { defaultValue: key }) }))}
              onChange={strategy => setDrafts(d => ({ ...d, [item.id]: { ...value, strategy } }))} />
            <Select mode="multiple" allowClear showSearch optionFilterProp="label" disabled={!canWrite}
              style={{ minWidth: 240, maxWidth: '100%' }} value={value.documents} options={documents}
              placeholder={t('wikiScenarios.allDocuments')} aria-label={t('wikiScenarios.scope')}
              onChange={ids => setDrafts(d => ({ ...d, [item.id]: { ...value, documents: ids } }))} />
            {item.enabled && <Button disabled={!canWrite} onClick={() => void configure(item, true)}>{t('wikiScenarios.saveConfig')}</Button>}
            {item.enabled && item.available_operations.includes('resources') && <Button onClick={() => void read(item.id)}>{t('wikiScenarios.open')}</Button>}
            {item.enabled && canWrite && item.available_operations.includes('annotate') &&
              <Button onClick={() => void manual(item)}>{t('wikiScenarios.annotate')}</Button>}
            {item.enabled && canWrite && ['ready', 'failed'].includes(item.status) &&
              <Button onClick={async () => { try { await runWikiScenario(kbId, item.id); await load(); }
                catch (e) { setError((e as Error).message); } }}>{t('wikiScenarios.interpret')}</Button>}
          </Space>
          {item.error && <Alert type="warning" message={item.error} style={{ marginTop: 12 }} />}
        </div>;
      })}
      {state && !state.items.length && <Empty description={t('wikiScenarios.noScenarios')} />}
      {selected && resources && <div style={{ marginTop: 28 }}>
        <Alert type="info" showIcon message={t('wikiScenarios.coverage', { processed: resources.coverage.processed, total: resources.coverage.total })} />
        {!!resources.coverage.rejected_candidates && <Alert type="warning" showIcon style={{ marginTop: 12 }}
          message={t('wikiScenarios.rejectedCandidates', { count: resources.coverage.rejected_candidates })} />}
        {resources.items.map(item => <div key={item.object_id} style={{ padding: '20px 0', borderBottom: '1px solid #e7ebf0' }}>
          <Space wrap><Typography.Text strong>{item.label}</Typography.Text>
            <Tag>{state?.items.find(s => s.id === selected)?.kind_labels[item.kind.slice(selected.length + 1)] || item.kind}</Tag>
            <Tag>{t('wikiScenarios.reviewStatuses.' + item.review_status, { defaultValue: item.review_status })}</Tag>
            {item.origin && <Tag>{t('wikiScenarios.origins.' + item.origin.kind, { defaultValue: item.origin.kind })}</Tag>}
          </Space>
          <Typography.Paragraph style={{ marginTop: 8, whiteSpace: 'pre-wrap' }}>{item.interpretation}</Typography.Paragraph>
          {item.fragments.map((f, index) => <blockquote key={index} style={{ margin: '12px 0', padding: '10px 16px', borderLeft: '3px solid #a5b9e7', background: '#f7f9fc' }}>
            <div style={{ whiteSpace: 'pre-wrap' }}>{f.text || t('wikiScenarios.visualCandidate')}</div>
            <Button type="link" onClick={() => void original(f)}>{t('wikiScenarios.original')}</Button>
          </blockquote>)}
          {!!item.missing.length && <Alert type="warning" message={item.missing.join('；')} />}
          {!!item.uncertainties.length && <Alert type="warning" message={item.uncertainties.join('；')} />}
          {canWrite && <Space style={{ marginTop: 12 }}>{(['accepted', 'rejected'] as const).map(status =>
            <Button key={status} onClick={async () => { try { await reviewWikiScenario(kbId, selected,
              { expected_version: resources.review_version, object_id: item.object_id, status }); await read(selected); }
              catch (e) { setError((e as Error).message); } }}>{t('wikiScenarios.' + status)}</Button>)}</Space>}
        </div>)}
        <Space wrap style={{ marginTop: 16 }}>
          <Typography.Text type="secondary">{resources.total ? offset + 1 : 0}–{offset + resources.items.length} / {resources.total}</Typography.Text>
          <Button disabled={busy || offset === 0} onClick={() => void read(selected, Math.max(0, offset - 50))}>{t('wikiScenarios.previous')}</Button>
          <Button disabled={busy || !resources.has_more} onClick={() => void read(selected, offset + 50)}>{t('wikiScenarios.next')}</Button>
        </Space>
      </div>}
    </Spin>
    <Modal open={Boolean(annotation)} title={t('wikiScenarios.annotate')} width={900}
      onCancel={() => setAnnotation(undefined)} onOk={() => void saveAnnotation()} okText={t('wikiScenarios.saveAnnotation')}>
      {annotation && <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Alert type="info" message={t('wikiScenarios.annotationHint')} />
        <Select style={{ width: '100%' }} aria-label={t('wikiScenarios.annotationSource')} value={annotation.unit?.unit_id}
          placeholder={t('wikiScenarios.annotationSource')} onChange={id => void annotationUnit(id)}
          options={annotation.units.map(u => ({ value: u.unit_id, label: u.source_name + ' · ' + u.modality + ' · ' + (u.preview || '') }))} />
        <Space wrap>
          <Button disabled={!annotation.offset} onClick={() => void manual(annotation.scene, Math.max(0, annotation.offset - 50))}>{t('wikiScenarios.previous')}</Button>
          <Typography.Text type="secondary">{annotation.offset} / {annotation.total}</Typography.Text>
          <Button disabled={annotation.offset + 50 >= annotation.total} onClick={() => void manual(annotation.scene, annotation.offset + 50)}>{t('wikiScenarios.next')}</Button>
        </Space>
        {annotation.unit && <>
          <Button onClick={() => void original(annotation.unit!)}>{t('wikiScenarios.original')}</Button>
          {annotation.unit.modality === 'text' && <>
            <textarea ref={sourceSelection} readOnly value={annotation.unit.text} aria-label={t('wikiScenarios.materialText')}
              style={{ width: '100%', height: 220, padding: 12 }} />
            <Button onClick={() => { const box = sourceSelection.current; if (box) setAnnotation(a => a ? { ...a,
              quote: box.value.slice(box.selectionStart, box.selectionEnd) } : a); }}>{t('wikiScenarios.quoteSelection')}</Button>
            <Input.TextArea aria-label={t('wikiScenarios.annotationQuote')} value={annotation.quote}
              placeholder={t('wikiScenarios.annotationQuote')} onChange={e => setAnnotation({ ...annotation, quote: e.target.value })} />
          </>}
        </>}
        <Select style={{ width: '100%' }} aria-label={t('wikiScenarios.annotationKind')} value={annotation.kind}
          options={annotation.scene.kinds.map(k => ({ value: k, label: annotation.scene.kind_labels[k] || k }))}
          onChange={kind => setAnnotation({ ...annotation, kind })} />
        <Input aria-label={t('wikiScenarios.annotationLabel')} value={annotation.label} placeholder={t('wikiScenarios.annotationLabel')}
          onChange={e => setAnnotation({ ...annotation, label: e.target.value })} />
        <Input.TextArea aria-label={t('wikiScenarios.annotationInterpretation')} value={annotation.interpretation}
          placeholder={t('wikiScenarios.annotationInterpretation')} onChange={e => setAnnotation({ ...annotation, interpretation: e.target.value })} />
      </Space>}
    </Modal>
    <Modal open={Boolean(preview)} title={t('wikiScenarios.original')} footer={null} width={1000} onCancel={() => setPreview(undefined)}>
      {preview && <>
        {preview.image ? <div style={{ position: 'relative' }}><img src={preview.url} alt={t('wikiScenarios.original')} style={{ width: '100%', display: 'block' }} />
          {preview.bbox && <div style={{ position: 'absolute', pointerEvents: 'none', border: '2px solid #d98c14',
            left: preview.bbox[0] * 100 + '%', top: preview.bbox[1] * 100 + '%',
            width: (preview.bbox[2] - preview.bbox[0]) * 100 + '%', height: (preview.bbox[3] - preview.bbox[1]) * 100 + '%' }} />}
        </div> : <iframe src={preview.url} title={t('wikiScenarios.original')} style={{ width: '100%', height: '65vh', border: 0 }} />}
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginTop: 16 }}>{preview.text}</Typography.Paragraph>
        <Typography.Link href={preview.url} target="_blank" rel="noopener noreferrer">{t('wikiScenarios.openOriginal')}</Typography.Link>
      </>}
    </Modal>
  </div>;
}
