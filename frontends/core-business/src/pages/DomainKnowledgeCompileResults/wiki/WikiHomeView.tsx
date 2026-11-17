/**
 * [jonex] Wiki 阅读界面首页：主题卡片 + 目录状态（生成/调整/过期提示）。
 *
 * 未生成目录时不留白：直接按引擎分类目录给出可点页面列表，
 * 并在有写权限时引导「生成目录」（AI 起草阅读顺序）。
 */
import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Card, Empty, Popconfirm, Tag } from 'antd';
import {
  BulbOutlined,
  FileTextOutlined,
  ReloadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import type { WikiOutline, WikiReaderPage } from '@/types/domainKnowledge';
import { countEntries, roleLabelKey } from './wikiMarkdown';

interface WikiHomeViewProps {
  pages: WikiReaderPage[];
  outline: WikiOutline | null;
  stale: boolean;
  canWrite: boolean;
  busy: boolean;
  generatedAt: string | null;
  onOpenTopic: (nodeId: string) => void;
  onOpenPage: (path: string) => void;
  onDraft: () => void;
  onAdjust: () => void;
}

export default function WikiHomeView({
  pages,
  outline,
  stale,
  canWrite,
  busy,
  generatedAt,
  onOpenTopic,
  onOpenPage,
  onDraft,
  onAdjust,
}: WikiHomeViewProps) {
  const { t } = useTranslation();
  const [expandedCategory, setExpandedCategory] = useState<string>('');

  const categoryGroups = useMemo(() => {
    const groups = new Map<string, WikiReaderPage[]>();
    for (const page of pages) {
      const key = page.category || t('domainKnowledge.wikiReader.unclassified');
      groups.set(key, [...(groups.get(key) || []), page]);
    }
    return Array.from(groups.entries()).sort((a, b) => b[1].length - a[1].length);
  }, [pages, t]);

  const topics = outline?.topics || [];

  return (
    <div>
      {/* 未生成目录：引导 + 分类兜底 */}
      {!outline && (
        <Card
          style={{ borderRadius: 12, border: '1px dashed #cbd5e1', background: '#fbfdff' }}
          styles={{ body: { padding: '20px 24px' } }}
        >
          <div style={{ display: 'flex', gap: 14, alignItems: 'flex-start' }}>
            <div style={{ fontSize: 22, color: 'var(--yx-accent)' }}>
              <BulbOutlined />
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 16, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>
                {t('domainKnowledge.wikiReader.emptyTitle')}
              </div>
              <div style={{ fontSize: 13, color: 'var(--yx-text-secondary)', marginTop: 6, lineHeight: 1.8 }}>
                {t('domainKnowledge.wikiReader.emptyDesc')}
              </div>
              <div style={{ marginTop: 12 }}>
                {canWrite ? (
                  <Button type="primary" icon={<ThunderboltOutlined />} loading={busy} onClick={onDraft}>
                    {t('domainKnowledge.wikiReader.generate')}
                  </Button>
                ) : (
                  <span style={{ fontSize: 12, color: 'var(--yx-text-muted)' }}>
                    {t('domainKnowledge.wikiReader.needWritePermission')}
                  </span>
                )}
              </div>
            </div>
          </div>
        </Card>
      )}

      {/* 目录已生成：总览 + 主题卡片 */}
      {outline && (
        <div>
          {stale && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 14, borderRadius: 8 }}
              message={t('domainKnowledge.wikiReader.staleTitle')}
              description={t('domainKnowledge.wikiReader.staleDesc')}
              action={
                canWrite ? (
                  <Popconfirm
                    title={t('domainKnowledge.wikiReader.regenerateConfirm')}
                    onConfirm={onDraft}
                    okText={t('common.confirm')}
                    cancelText={t('common.cancel')}
                  >
                    <Button size="small" icon={<ReloadOutlined />} loading={busy}>
                      {t('domainKnowledge.wikiReader.regenerate')}
                    </Button>
                  </Popconfirm>
                ) : undefined
              }
            />
          )}

          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
            <h2 style={{ margin: 0, fontSize: 22, color: 'var(--yx-brand-dark)' }}>
              {t('domainKnowledge.wikiReader.outlineTitle')}
            </h2>
            <span style={{ color: 'var(--yx-text-muted)', fontSize: 13 }}>
              {t('domainKnowledge.wikiReader.outlineStats', {
                topics: topics.length,
                pages: pages.length,
              })}
            </span>
            {generatedAt && (
              <span style={{ color: '#cbd5e1', fontSize: 12 }}>
                {/* [jonex] 只报生成时间：生成所用的 LLM 模型名属内部实现，不对最终用户暴露 */}
                {t('domainKnowledge.wikiReader.generatedMeta', {
                  time: new Date(generatedAt).toLocaleString(),
                })}
              </span>
            )}
          </div>

          {outline.overview && (
            <div style={{ marginTop: 8, fontSize: 14, color: '#475569', lineHeight: 1.8 }}>{outline.overview}</div>
          )}

          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
              gap: 14,
              marginTop: 16,
            }}
          >
            {topics.map((topic) => {
              const count = countEntries(topic);
              const preview = collectPreviewEntries(topic, 4);
              return (
                <Card
                  key={topic.id}
                  hoverable
                  onClick={() => onOpenTopic(topic.id)}
                  style={{
                    borderRadius: 12,
                    border: '1px solid var(--yx-border-light)',
                    opacity: topic.kind === 'unclassified' ? 0.92 : 1,
                  }}
                  styles={{ body: { padding: '16px 18px' } }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--yx-brand-dark)', flex: 1 }}>
                      {topic.kind === 'unclassified' ? t('domainKnowledge.wikiReader.unclassified') : topic.title}
                    </span>
                    <Tag
                      color={topic.kind === 'unclassified' ? 'default' : 'blue'}
                      style={{ margin: 0, borderRadius: 6 }}
                    >
                      {t('domainKnowledge.wikiReader.pageCount', { count })}
                    </Tag>
                  </div>
                  {topic.description && (
                    <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', marginTop: 6, lineHeight: 1.7 }}>
                      {topic.description}
                    </div>
                  )}
                  <div style={{ marginTop: 10 }}>
                    {preview.map((entry) => (
                      <div
                        key={entry.path}
                        style={{
                          fontSize: 13,
                          color: '#2563eb',
                          lineHeight: 1.9,
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                        }}
                        onClick={(event) => {
                          event.stopPropagation();
                          onOpenPage(entry.path);
                        }}
                      >
                        · {entry.title}
                      </div>
                    ))}
                    {count > preview.length && (
                      <div style={{ fontSize: 12, color: '#cbd5e1', marginTop: 4 }}>
                        {t('domainKnowledge.wikiReader.moreEntries', {
                          count: count - preview.length,
                        })}
                      </div>
                    )}
                  </div>
                </Card>
              );
            })}
          </div>
        </div>
      )}

      {/* 分类兜底浏览（未生成目录时的主要入口；已生成时作为附录） */}
      {!outline && (
        <div style={{ marginTop: 20 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--yx-brand-dark)', marginBottom: 10 }}>
            <FileTextOutlined /> {t('domainKnowledge.wikiReader.browseByCategory')}
          </div>
          {categoryGroups.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('domainKnowledge.wikiReader.noPages')} />
          ) : (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
                gap: 12,
              }}
            >
              {categoryGroups.map(([category, items]) => (
                <Card
                  key={category}
                  hoverable
                  onClick={() => setExpandedCategory((prev) => (prev === category ? '' : category))}
                  style={{ borderRadius: 12, border: '1px solid var(--yx-border-light)' }}
                  styles={{ body: { padding: '14px 16px' } }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--yx-brand-dark)', flex: 1 }}>{category}</span>
                    <Tag style={{ margin: 0 }}>{items.length}</Tag>
                  </div>
                  {expandedCategory === category && (
                    <div style={{ marginTop: 10, maxHeight: 260, overflowY: 'auto' }}>
                      {items.map((page) => (
                        <div
                          key={page.path}
                          style={{ fontSize: 13, color: '#2563eb', lineHeight: 1.9 }}
                          onClick={(event) => {
                            event.stopPropagation();
                            onOpenPage(page.path);
                          }}
                        >
                          <span style={{ color: 'var(--yx-text-muted)', marginRight: 6 }}>[{t(roleLabelKey(page.type))}]</span>
                          {page.title}
                        </div>
                      ))}
                    </div>
                  )}
                </Card>
              ))}
            </div>
          )}
        </div>
      )}

      {outline && canWrite && (
        <div style={{ marginTop: 18 }}>
          <Button icon={<RobotOutlined />} onClick={onAdjust} loading={busy}>
            {t('domainKnowledge.wikiReader.adjust')}
          </Button>
          <span style={{ fontSize: 12, color: 'var(--yx-text-muted)', marginLeft: 10 }}>
            {t('domainKnowledge.wikiReader.adjustHint')}
          </span>
        </div>
      )}
    </div>
  );
}

/** 取节点下前 N 个条目（跨小节，按 order）。 */
function collectPreviewEntries(node: WikiOutline['topics'][number], limit: number): { path: string; title: string }[] {
  const out: { path: string; title: string }[] = [];
  const walk = (current: WikiOutline['topics'][number]) => {
    for (const entry of current.entries || []) {
      if (out.length >= limit) return;
      out.push({ path: entry.path, title: entry.title });
    }
    for (const child of current.children || []) walk(child);
  };
  walk(node);
  return out;
}
