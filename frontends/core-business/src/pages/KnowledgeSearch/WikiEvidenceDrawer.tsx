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
  // 显式分层：叠放顺序不能靠 portal 的增删顺序（2026-10-30 实测复现的错位就出在这里）。
  // 两层抽屉都用 antd 默认 z-index（1000）时，谁在上面只由 DOM 里谁后出现决定；而
  // 「引用详情」带 `destroyOnHidden` —— 每次打开都会重建 portal 并追加到 body 末尾，
  // 「原文证据」（本组件）不带，portal 一直留着。于是「关掉两层再重开」的第二轮里，
  // 引用详情反而压到原文证据之上（用户看到的就是原文证据掉到下面）。
  // 固定成：引用详情 1000（antd 默认）→ 原文证据 1100 → 文档查看弹层 1200（DocumentViewer）。
  // 三者都低于 antd message / notification（zIndexPopupBase + CONTAINER_MAX_OFFSET + 10
  // = 2110），下载失败这类轻提示仍浮在最上面。
  return <Drawer open={!!reference} onClose={onClose} width="min(880px, 100vw)"
    title={t('knowledgeSearch.wikiEvidenceTitle')}
    zIndex={1100}>
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
