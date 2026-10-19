import React from 'react';
import { useTranslation } from 'react-i18next';
import MarkdownContent from '@/components/MarkdownContent';
import type { KnowledgeReference } from '@/types/knowledgeSearch';
import { isMarkdownSnippet, snippetLocation } from './citationIndex';
import styles from './WikiSearch.module.scss';

/** Readable presentation of an existing citation; the full original payload remains available. */
export default function WikiReferenceSnippet({ reference }: { reference: KnowledgeReference }) {
  const { t } = useTranslation();
  const snippet = snippetLocation(reference)?.text ?? '';
  const evidence = snippetLocation(reference)?.evidence_ref;
  if (evidence?.kind === 'table_cell') {
    try {
      const cell = JSON.parse(snippet);
      if (cell && typeof cell.coordinate === 'string' && (cell.value === null || ['string', 'number', 'boolean'].includes(typeof cell.value))) {
        return <div className={styles.cellSnippet}>
          <span>{t('knowledgeSearch.wikiCellPosition', { coordinate: cell.coordinate })}</span>
          <strong>{cell.value === null ? t('knowledgeSearch.wikiEmptyCell') : String(cell.value)}</strong>
          <details><summary>{t('knowledgeSearch.wikiOriginalCitation')}</summary><pre>{snippet}</pre></details>
        </div>;
      }
    } catch { /* Older plain-text citations retain their original presentation. */ }
  }
  return isMarkdownSnippet(reference.file_name, snippet) || /<table[\s>]/i.test(snippet)
    ? <MarkdownContent content={snippet} /> : <div className={styles.plainExcerpt}>{snippet}</div>;
}
