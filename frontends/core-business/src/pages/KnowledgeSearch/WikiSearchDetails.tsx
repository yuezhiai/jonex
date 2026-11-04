import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CaretDownOutlined, CaretRightOutlined, FileTextOutlined } from '@ant-design/icons';
import type { KnowledgeReference } from '@/types/knowledgeSearch';
import { snippetLocation } from './citationIndex';
import WikiReferenceSnippet from './WikiReferenceSnippet';
import styles from './WikiSearch.module.scss';

/**
 * Wiki 来源卡片。
 *
 * 检索轨迹已迁到 `WikiTrace.tsx`（流式与历史回放同一份数据、同一套渲染），
 * 这里只保留「来源 / 引用」这一块，避免一个文件同时管轨迹和证据两件事。
 */
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
