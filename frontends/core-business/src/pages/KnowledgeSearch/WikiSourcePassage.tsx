import React, { useState } from 'react';
import { Segmented } from 'antd';
import { useTranslation } from 'react-i18next';
import MarkdownContent from '@/components/MarkdownContent';
import styles from './WikiSearch.module.scss';

export default function WikiSourcePassage({ segments }: {
  segments: { text: string; highlight: boolean }[];
}) {
  const { t } = useTranslation();
  const [mode, setMode] = useState('reading');
  return <>
    <Segmented size="small" value={mode} onChange={(value) => setMode(String(value))}
      options={[{ label: t('knowledgeSearch.wikiReadingView'), value: 'reading' },
        { label: t('knowledgeSearch.wikiRawView'), value: 'raw' }]} />
    {mode === 'raw' ? <div className={styles.sourceText}>
      {segments.map((segment, index) => segment.highlight ? <mark key={index}>{segment.text}</mark>
        : <span key={index}>{segment.text}</span>)}
    </div> : <div className={styles.sourcePassage}>
      {segments.filter((segment) => segment.text).map((segment, index) =>
        <div key={index} className={segment.highlight ? styles.highlightedPassage : styles.passageContext}>
          <MarkdownContent content={segment.text} />
        </div>)}
    </div>}
  </>;
}
