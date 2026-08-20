import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from 'antd';
import { HistoryOutlined } from '@ant-design/icons';
import type { KnowledgeSearchHistoryItem } from '@/types/knowledgeSearch';

function formatRelativeTime(isoStr: string, t: (key: string, opts?: any) => string): string {
  const now = Date.now();
  const then = new Date(isoStr).getTime();
  const diffMs = now - then;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return t('common.justNow');
  if (diffMin < 60) return t('common.minutesAgo', { count: diffMin });
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return t('common.hoursAgo', { count: diffHr });
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay === 1) return t('common.yesterday');
  if (diffDay < 7) return t('common.daysAgo', { count: diffDay });
  return isoStr.substring(0, 10);
}

interface SearchHistorySidebarProps {
  history: KnowledgeSearchHistoryItem[];
  activeHistoryIndex: number | null;
  onHistoryClick: (item: KnowledgeSearchHistoryItem, index: number) => void;
  onDeleteHistory: (id: string, index: number) => void;
  onClearHistory: () => void;
  // [jonex] 历史分页："查看更多"按钮（已加载条数 < 总数时展示）
  hasMore?: boolean;
  onLoadMore?: () => void;
  loadingMore?: boolean;
}

export default function SearchHistorySidebar({
  history,
  activeHistoryIndex,
  onHistoryClick,
  onDeleteHistory,
  onClearHistory,
  hasMore,
  onLoadMore,
  loadingMore,
}: SearchHistorySidebarProps) {
  const { t } = useTranslation();

  return (
    <div
      style={{
        width: 280,
        flexShrink: 0,
        background: '#fff',
        borderRadius: 12,
        border: '1px solid #eef2f6',
        boxShadow: '0 1px 3px rgba(0,0,0,0.03)',
        overflow: 'hidden',
        position: 'sticky',
        top: 28,
        // 固定高度：顶部吸顶 28px + 底部留白 272px，内容区内部上下滚动
        height: 'calc(100vh - 300px)',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <div
        style={{
          padding: '16px 20px',
          fontSize: 15,
          fontWeight: 600,
          color: '#0b2b5c',
          borderBottom: '1px solid #f1f5f9',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flexShrink: 0,
        }}
      >
        <HistoryOutlined />
        {t('knowledgeSearch.searchHistory')}
        {history.length > 0 && (
          <Button
            type="text"
            size="small"
            onClick={onClearHistory}
            style={{ marginLeft: 'auto', color: '#94a3b8', fontSize: 12 }}
          >
            {t('knowledgeSearch.clear')}
          </Button>
        )}
      </div>
      {history.length === 0 ? (
        <div
          style={{
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: '40px 20px',
            textAlign: 'center',
            color: '#94a3b8',
            fontSize: 13,
          }}
        >
          {t('knowledgeSearch.noSearchHistory')}
        </div>
      ) : (
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          {history.map((h, i) => (
          <div
            key={h.id}
            style={{
              padding: '14px 20px',
              borderBottom: i < history.length - 1 ? '1px solid #f8fafc' : 'none',
              cursor: 'pointer',
              transition: 'background 0.2s',
              background: activeHistoryIndex === i ? '#eff6ff' : undefined,
              borderLeft: activeHistoryIndex === i ? '3px solid #3b82f6' : '3px solid transparent',
              display: 'flex',
              alignItems: 'flex-start',
              gap: 8,
            }}
          >
            <div onClick={() => onHistoryClick(h, i)} style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  fontSize: 13,
                  fontWeight: 500,
                  color: '#1e293b',
                  marginBottom: 4,
                  lineHeight: 1.4,
                }}
              >
                {h.query}
              </div>
              <div style={{ display: 'flex', gap: 10, fontSize: 11, color: '#94a3b8' }}>
                <span>{t('knowledgeSearch.resultsCount', { count: h.resultCount })}</span>
                <span>{formatRelativeTime(h.searchedAt, t)}</span>
              </div>
            </div>
            <Button
              type="text"
              size="small"
              onClick={(e) => {
                e.stopPropagation();
                onDeleteHistory(h.id, i);
              }}
              style={{
                color: '#cbd5e1',
                fontSize: 14,
                flexShrink: 0,
                padding: '2px 4px',
                lineHeight: 1,
                height: 'auto',
              }}
              title={t('knowledgeSearch.deleteHistory')}
            >
              ×
            </Button>
          </div>
          ))}
          {hasMore && (
            <div style={{ padding: '12px 20px', textAlign: 'center' }}>
              <Button type="link" size="small" loading={loadingMore} onClick={onLoadMore}>
                {t('knowledgeSearch.loadMore')}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
