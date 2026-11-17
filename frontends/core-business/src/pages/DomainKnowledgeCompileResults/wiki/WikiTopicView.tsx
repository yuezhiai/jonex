/**
 * [jonex] Wiki 阅读界面「主题页」：像课本目录一样的有序章节列表。
 *
 * 一个主题 = 若干小节（可嵌套）+ 有序条目；条目编号在主题内连续，
 * 让读者一眼看出「先读什么、后读什么」——这正是引擎分类目录给不了的信息。
 */
import React from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Empty, Tag } from 'antd';
import { FileTextOutlined } from '@ant-design/icons';
import type { WikiOutlineNode, WikiReaderPage } from '@/types/domainKnowledge';
import { countEntries, roleLabelKey } from './wikiMarkdown';

interface WikiTopicViewProps {
  node: WikiOutlineNode;
  pagesByPath: Map<string, WikiReaderPage>;
  onOpenPage: (path: string) => void;
  /** 目录允许调整时展示「调整目录」引导 */
  onRequestAdjust?: () => void;
}

export default function WikiTopicView({
  node,
  pagesByPath,
  onOpenPage,
  onRequestAdjust,
}: WikiTopicViewProps) {
  const { t } = useTranslation();
  // 编号在整个主题内连续（跨小节不重置），与课本目录一致
  let counter = 0;

  const renderSection = (section: WikiOutlineNode, depth: number) => (
    <div key={section.id} style={{ marginTop: depth === 0 ? 20 : 14, marginLeft: depth * 12 }}>
      <div
        style={{
          fontSize: depth === 0 ? 15 : 14,
          fontWeight: 600,
          color: 'var(--yx-brand-dark)',
          borderLeft: '3px solid var(--yx-accent)',
          paddingLeft: 10,
          marginBottom: 6,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <span>{section.title}</span>
        <span style={{ color: '#cbd5e1', fontSize: 12, fontWeight: 400 }}>
          {countEntries(section)}
        </span>
      </div>
      {section.description && (
        <div style={{ fontSize: 13, color: 'var(--yx-text-secondary)', paddingLeft: 13, marginBottom: 6 }}>
          {section.description}
        </div>
      )}
      <div>{renderEntries(section)}</div>
      {(section.children || []).map((child) => renderSection(child, depth + 1))}
    </div>
  );

  const renderEntries = (owner: WikiOutlineNode) => {
    const entries = [...(owner.entries || [])].sort((a, b) => a.order - b.order);
    if (entries.length === 0) return null;
    return (
      <div>
        {entries.map((entry) => {
          counter += 1;
          const page = pagesByPath.get(entry.path);
          return (
            <div
              key={entry.path}
              onClick={() => onOpenPage(entry.path)}
              style={{
                display: 'flex',
                gap: 10,
                alignItems: 'baseline',
                padding: '6px 10px',
                borderRadius: 8,
                cursor: 'pointer',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = 'var(--yx-bg-subtle)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'transparent';
              }}
            >
              <span
                style={{
                  minWidth: 22,
                  color: 'var(--yx-text-muted)',
                  fontSize: 12,
                  fontVariantNumeric: 'tabular-nums',
                }}
              >
                {counter}.
              </span>
              <span style={{ flex: 1 }}>
                <span style={{ color: '#1d4ed8', fontSize: 14 }}>{entry.title}</span>
                <Tag
                  style={{ marginLeft: 8, fontSize: 11, lineHeight: '16px' }}
                  color={entry.role === 'concept' ? 'blue' : entry.role === 'table' ? 'purple' : 'default'}
                >
                  {t(roleLabelKey(entry.role))}
                </Tag>
                {(entry.note || page?.description) && (
                  <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', marginTop: 2 }}>
                    {entry.note || page?.description}
                  </div>
                )}
              </span>
            </div>
          );
        })}
      </div>
    );
  };

  const isEmpty = countEntries(node) === 0;

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: 22, color: 'var(--yx-brand-dark)' }}>
          {node.kind === 'unclassified' ? t('domainKnowledge.wikiReader.unclassified') : node.title}
        </h2>
        <span style={{ color: 'var(--yx-text-muted)', fontSize: 13 }}>
          {t('domainKnowledge.wikiReader.topicEntries', { count: countEntries(node) })}
        </span>
      </div>
      {node.description && (
        <div style={{ marginTop: 6, fontSize: 14, color: '#475569' }}>{node.description}</div>
      )}

      {node.kind === 'unclassified' && (
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 12, borderRadius: 8 }}
          message={t('domainKnowledge.wikiReader.unclassifiedHint')}
          action={
            onRequestAdjust ? (
              <a onClick={onRequestAdjust}>{t('domainKnowledge.wikiReader.adjust')}</a>
            ) : undefined
          }
        />
      )}

      {isEmpty ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('domainKnowledge.wikiReader.topicEmpty')}
          style={{ marginTop: 32 }}
        />
      ) : (
        <div>
          {renderEntries(node)}
          {(node.children || []).map((child) => renderSection(child, 0))}
        </div>
      )}

      <div style={{ marginTop: 24, fontSize: 12, color: '#cbd5e1' }}>
        <FileTextOutlined /> {t('domainKnowledge.wikiReader.outlineOnlyHint')}
      </div>
    </div>
  );
}
