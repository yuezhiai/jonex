import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CaretDownOutlined, CaretRightOutlined, LoadingOutlined, ToolOutlined,
  FileSearchOutlined, ReadOutlined, TableOutlined, FileTextOutlined, CheckOutlined, NodeIndexOutlined } from '@ant-design/icons';
import type { KnowledgeReference, WikiProgressEvent } from '@/types/knowledgeSearch';
import { snippetLocation } from './citationIndex';
import WikiReferenceSnippet from './WikiReferenceSnippet';
import { wikiProgressEntries } from './wikiPresentation';
import styles from './WikiSearch.module.scss';

export function WikiProgress({ events, running, completed, expanded, onToggle, startedAt, durationMs, source }: {
  events: WikiProgressEvent[]; running: boolean; completed: boolean; expanded: boolean; onToggle: () => void;
  startedAt?: number; durationMs?: number | null;
  source?: string;
}) {
  const { t } = useTranslation();
  const [now, setNow] = useState(Date.now());
  const [liveExpanded, setLiveExpanded] = useState(true);
  useEffect(() => {
    if (!running) return;
    setLiveExpanded(true);
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, startedAt]);
  const entries = useMemo(() => wikiProgressEntries(events), [events]);
  const open = running ? liveExpanded : expanded;
  const elapsed = running && startedAt != null ? Math.max(0, now - startedAt) : durationMs;
  const seconds = elapsed != null ? Math.floor(Math.max(0, elapsed) / 1000) : null;
  const time = seconds == null ? '' : seconds >= 60
    ? t('knowledgeSearch.wikiMinutesSeconds', { minutes: Math.floor(seconds / 60), seconds: seconds % 60 })
    : t('knowledgeSearch.wikiSeconds', { seconds });
  const icons = { search: FileSearchOutlined, read: ReadOutlined, table: TableOutlined,
    analyze: ToolOutlined, answer: FileTextOutlined };
  return <section className={styles.progress} aria-label={t('knowledgeSearch.reasoningProcess')}>
    <button type="button" className={styles.progressSummary}
      onClick={() => running ? setLiveExpanded((value) => !value) : onToggle()} aria-expanded={open}>
      {open ? <CaretDownOutlined /> : <CaretRightOutlined />}
      <NodeIndexOutlined className={styles.progressIcon} />
      <strong>{t('knowledgeSearch.reasoningProcess')}</strong>
      <span className={styles.progressMeta}>{source
        ? t('knowledgeSearch.steps', { count: entries.length, source }) : ''}{time ? ` · ${time}` : ''}</span>
      {running && <LoadingOutlined />}
      {!running && !completed && <span className={styles.progressMeta}>{t('knowledgeSearch.wikiInterrupted')}</span>}
    </button>
    {open && <div className={styles.progressFeed}>
      {entries.map((entry, i) => {
        const Icon = icons[entry.category];
        const active = running && !entry.done && i === entries.length - 1;
        const detail = entry.details;
        return <div key={entry.id} className={styles.progressItem}>
          <div className={styles.progressStep}>
            <Icon aria-hidden="true" />
            <span>{t(`knowledgeSearch.wikiAction.${entry.category}`)}</span>
            {active ? <LoadingOutlined className={styles.stepStatus} />
              : entry.done ? <CheckOutlined className={styles.stepStatus} /> : null}
            {entry.durationMs != null && <small>{(entry.durationMs / 1000).toFixed(2)} s</small>}
          </div>
          {detail && <div className={styles.progressContent}>
            {detail.query && <p><span>{t('knowledgeSearch.wikiSearchTerms')}</span>{detail.query}</p>}
            {!detail.previews?.length && detail.paths?.map((path, n) => <p key={n} className={styles.progressPath}>{path}</p>)}
            {detail.table_id && <p><span>{t('knowledgeSearch.wikiTable')}</span>{detail.table_id}</p>}
            {detail.range && <p><span>{t('knowledgeSearch.wikiRange')}</span>{detail.range.join(' – ')}</p>}
            {detail.operation && <p><span>{t('knowledgeSearch.wikiOperation')}</span>{detail.operation}</p>}
            {detail.previews?.map((preview, n) => <div key={n} className={styles.progressPreview}>
              {(preview.title || preview.path) && <strong title={preview.path}>{preview.title || preview.path}</strong>}
              {preview.excerpt && <p className={styles.progressExcerpt}>{preview.excerpt}
                <span className={styles.previewLabel}>{t('knowledgeSearch.wikiSnippetPreview')}</span></p>}
            </div>)}
          </div>}
        </div>;
      })}
      {running && !entries.length && <div className={styles.progressStep}>
        <FileSearchOutlined /><span>{t('knowledgeSearch.wikiStarting')}</span>
      </div>}
    </div>}
  </section>;
}

function WikiSourceCard({ items, activeIndex, onSelect, onOpen }: {
  items: { ref: KnowledgeReference; index: number }[]; activeIndex?: number | null;
  onSelect: (index: number) => void; onOpen: (reference: KnowledgeReference) => void;
}) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState(items[0].index);
  const active = items.find((item) => item.index === activeIndex)
    ?? items.find((item) => item.index === selected) ?? items[0];
  const reference = active.ref;
  const snippet = snippetLocation(reference)?.text ?? '';
  const extension = reference.file_name.split('.').pop()?.toUpperCase();
  const format = extension && extension.length <= 8 ? extension : reference.media_type.toUpperCase();
  return <article className={styles.sourceCard}>
    <header className={styles.sourceHeader}>
      <span className={styles.fileFormat}>{format}</span>
      <strong title={reference.file_name}>{reference.file_name}</strong>
      <span className={styles.sourceKb}>{t('knowledgeSearch.sourceKb', { name: reference.kb_name || reference.kb_id })}</span>
      <button type="button" className={styles.openSource} onClick={() => onOpen(reference)}>
        {t('knowledgeSearch.viewOriginal')}
      </button>
    </header>
    <nav className={styles.sourceCitations} aria-label={t('knowledgeSearch.citationDetail')}>
      {items.map(({ ref, index }) => <button type="button" key={index}
        className={active.index === index ? styles.selectedCitation : undefined}
        aria-pressed={active.index === index}
        onClick={() => { setSelected(index); onSelect(index); }}>
        {t('knowledgeSearch.wikiEvidenceNumber', { number: index + 1 })}
      </button>)}
    </nav>
    {snippet && <div className={styles.sourceExcerpt}>
      <WikiReferenceSnippet reference={reference} />
    </div>}
  </article>;
}

export function WikiSources({ references, expanded, onToggle, onOpen, onSelect, activeIndex }: {
  references: KnowledgeReference[]; expanded: boolean; onToggle: () => void;
  onOpen: (reference: KnowledgeReference) => void; onSelect: (index: number) => void; activeIndex?: number | null;
}) {
  const { t } = useTranslation();
  const groups = useMemo(() => {
    const result = new Map<string, { ref: KnowledgeReference; index: number }[]>();
    references.forEach((ref, index) => {
      const key = `${ref.kb_id}:${ref.doc_id}`;
      const group = result.get(key) ?? [];
      group.push({ ref, index });
      result.set(key, group);
    });
    return [...result.entries()];
  }, [references]);
  return <section className={styles.sources}>
    <button type="button" className={styles.sourcesSummary} onClick={onToggle} aria-expanded={expanded}>
      {expanded ? <CaretDownOutlined /> : <CaretRightOutlined />} <FileTextOutlined />
      <span>{t('knowledgeSearch.relatedDocs', { count: groups.length })}</span>
    </button>
    {expanded && <div className={styles.sourceList}>{groups.map(([key, items]) =>
      <WikiSourceCard key={key} items={items} activeIndex={activeIndex} onSelect={onSelect} onOpen={onOpen} />)}</div>}
  </section>;
}
