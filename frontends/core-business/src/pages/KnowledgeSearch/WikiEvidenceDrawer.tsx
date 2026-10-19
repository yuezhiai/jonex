import React, { useEffect, useState } from 'react';
import { Alert, Button, Drawer, Spin } from 'antd';
import { useTranslation } from 'react-i18next';
import { resolveWikiEvidence, type WikiEvidenceView } from '@/api/knowledgeSearch';
import type { KnowledgeReference } from '@/types/knowledgeSearch';
import styles from './WikiSearch.module.scss';
import WikiSourcePassage from './WikiSourcePassage';

export default function WikiEvidenceDrawer({ reference, onClose, onOriginal }: {
  reference: KnowledgeReference | null;
  onClose: () => void;
  onOriginal: (ref: KnowledgeReference, page?: number | null) => void;
}) {
  const { t } = useTranslation();
  const [view, setView] = useState<WikiEvidenceView | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    setView(null); setError('');
    if (!reference) return;
    const controller = new AbortController();
    resolveWikiEvidence(reference, controller.signal).then(setView).catch((e) => {
      if (!controller.signal.aborted) setError(e.message);
    });
    return () => controller.abort();
  }, [reference]);
  const cells = new Map(view?.cells?.map((c) => [`${c.row}:${c.column}`, c]) ?? []);
  return <Drawer open={!!reference} onClose={onClose} width="min(880px, 100vw)"
    title={t('knowledgeSearch.wikiEvidenceTitle')}>
    <div className={styles.evidenceHeader}>
      <strong>{reference?.file_name}</strong>
      {view && <span>{view.sheet_name || (view.page_number != null
        ? t('knowledgeSearch.docPage', { page: view.page_number }) : t('knowledgeSearch.wikiParsedSource'))}</span>}
      <Button onClick={() => reference && onOriginal(reference, view?.page_number)}>
        {t('knowledgeSearch.wikiOpenFile')}
      </Button>
    </div>
    {error && <Alert type="warning" showIcon title={error} />}
    {!error && !view && <Spin />}
    {view && <>
      {!view.exact_location && <Alert type="info" showIcon title={t('knowledgeSearch.wikiNoExactLocation')} />}
      {view.cells && <>
        <h3>{view.table_title}</h3>
        <p className={styles.note}>{t('knowledgeSearch.wikiTableContext')}</p>
        <div className={styles.tableViewport}>
          <table className={styles.sourceTable}>
            <thead><tr><th>#</th>{view.columns?.map((c) => <th key={c}>C{c}</th>)}</tr></thead>
            <tbody>{view.rows?.map((r) => <tr key={r}><th>{r}</th>{view.columns?.map((c) => {
              const cell = cells.get(`${r}:${c}`);
              return <td key={c} className={cell?.highlight ? styles.hitCell : undefined}
                title={[cell?.coordinate, cell?.formula].filter(Boolean).join(' · ')}>{cell?.value ?? ''}</td>;
            })}</tr>)}</tbody>
          </table>
        </div>
        <h3>{t('knowledgeSearch.wikiCitedExcerpt')}</h3>
      </>}
      <WikiSourcePassage segments={view.segments} />
      {view.truncated && <p className={styles.note}>{t('knowledgeSearch.wikiPartialContext')}</p>}
    </>}
  </Drawer>;
}
