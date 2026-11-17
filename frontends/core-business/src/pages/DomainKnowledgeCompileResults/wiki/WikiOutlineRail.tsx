/**
 * [jonex] Wiki 阅读界面左侧导航栏：目录树 + 全书搜索。
 *
 * 目录树 = LLM 起草的 Topic → Section → Entry（后端 knowledge_base.wiki_outlines）。
 * 未生成目录时退化为「按分类浏览」（引擎分类目录），保证任何编译产物都能读。
 */
import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Empty, Input, Tag, Tooltip } from 'antd';
import {
  CaretDownOutlined,
  CaretRightOutlined,
  FileTextOutlined,
  HomeOutlined,
  SearchOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons';
import type { WikiOutline, WikiOutlineNode, WikiReaderPage } from '@/types/domainKnowledge';
import { countEntries, roleLabelKey } from './wikiMarkdown';

interface WikiOutlineRailProps {
  pages: WikiReaderPage[];
  outline: WikiOutline | null;
  activePath: string;
  activeTopicId: string;
  onOpenHome: () => void;
  onOpenTopic: (nodeId: string) => void;
  onOpenPage: (path: string) => void;
  query: string;
  onQueryChange: (value: string) => void;
  searchResults: WikiReaderPage[];
  /** 目录生成信息（模型 / 时间），显示在栏底 */
  metaText?: string;
}

const entryStyle: React.CSSProperties = {
  fontSize: 13,
  color: '#475569',
  padding: '3px 6px',
  borderRadius: 4,
  cursor: 'pointer',
  display: 'block',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const groupTitleStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  fontSize: 13,
  fontWeight: 600,
  color: 'var(--yx-brand-dark)',
  padding: '6px 6px',
  cursor: 'pointer',
  borderRadius: 6,
};

export default function WikiOutlineRail({
  pages,
  outline,
  activePath,
  activeTopicId,
  onOpenHome,
  onOpenTopic,
  onOpenPage,
  query,
  onQueryChange,
  searchResults,
  metaText,
}: WikiOutlineRailProps) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  // 未生成目录时的分类兜底视图（引擎分类目录 → 页面清单）
  const categoryGroups = useMemo(() => {
    const groups = new Map<string, WikiReaderPage[]>();
    for (const page of pages) {
      const key = page.category || t('domainKnowledge.wikiReader.unclassified');
      groups.set(key, [...(groups.get(key) || []), page]);
    }
    return Array.from(groups.entries()).sort((a, b) => b[1].length - a[1].length);
  }, [pages, t]);

  const renderEntry = (path: string, title: string, role: string, note?: string) => (
    <Tooltip key={path} title={note ? `${title} · ${note}` : title} placement="right">
      <span
        style={{
          ...entryStyle,
          background: activePath === path ? 'var(--yx-info-bg)' : 'transparent',
          color: activePath === path ? '#1d4ed8' : '#475569',
          fontWeight: activePath === path ? 600 : 400,
        }}
        onClick={() => onOpenPage(path)}
        onMouseEnter={(e) => {
          if (activePath !== path) e.currentTarget.style.background = 'var(--yx-bg-subtle)';
        }}
        onMouseLeave={(e) => {
          if (activePath !== path) e.currentTarget.style.background = 'transparent';
        }}
      >
        {title}
        <Tag
          style={{ marginLeft: 6, fontSize: 11, lineHeight: '16px', transform: 'scale(0.92)' }}
          color={role === 'concept' ? 'blue' : role === 'table' ? 'purple' : 'default'}
        >
          {t(roleLabelKey(role))}
        </Tag>
      </span>
    </Tooltip>
  );

  const renderNode = (node: WikiOutlineNode, depth: number): React.ReactNode => {
    const isCollapsed = collapsed[node.id];
    const count = countEntries(node);
    return (
      <div key={node.id} style={{ marginLeft: depth * 10 }}>
        <div
          style={{
            ...groupTitleStyle,
            background: activeTopicId === node.id ? 'var(--yx-info-bg)' : 'transparent',
          }}
          onClick={() => {
            if (node.kind === 'topic' || node.kind === 'unclassified') onOpenTopic(node.id);
            else setCollapsed((prev) => ({ ...prev, [node.id]: !prev[node.id] }));
          }}
        >
          <span
            onClick={(event) => {
              event.stopPropagation();
              setCollapsed((prev) => ({ ...prev, [node.id]: !prev[node.id] }));
            }}
            style={{ color: 'var(--yx-text-muted)', fontSize: 10, width: 10 }}
          >
            {isCollapsed ? <CaretRightOutlined /> : <CaretDownOutlined />}
          </span>
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {node.kind === 'unclassified' ? t('domainKnowledge.wikiReader.unclassified') : node.title}
          </span>
          <span style={{ color: '#cbd5e1', fontSize: 11, fontWeight: 400 }}>{count}</span>
        </div>
        {!isCollapsed && (
          <div style={{ paddingLeft: 8 }}>
            {(node.children || []).map((child) => renderNode(child, depth + 1))}
            {(node.entries || []).map((entry) =>
              renderEntry(entry.path, entry.title, entry.role, entry.note),
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div
      style={{
        width: 268,
        flex: '0 0 268px',
        borderRight: '1px solid var(--yx-border-light)',
        padding: '12px 10px',
        // 撑满宿主的定高盒子并自己滚动——不再用 calc(100vh - N) 猜剩余空间
        // （那个值在知识库级 `?tab=assets` 与文档级两个宿主里都不成立）。
        // boxSizing：height:100% + padding 不写 border-box 会溢出 24px。
        boxSizing: 'border-box',
        height: '100%',
        overflowY: 'auto',
      }}
    >
      <div
        style={{
          ...groupTitleStyle,
          color: '#1d4ed8',
          marginBottom: 4,
        }}
        onClick={onOpenHome}
      >
        <HomeOutlined />
        <span>{t('domainKnowledge.wikiReader.home')}</span>
      </div>

      <Input
        size="small"
        allowClear
        prefix={<SearchOutlined style={{ color: 'var(--yx-text-muted)' }} />}
        placeholder={t('domainKnowledge.wikiReader.searchPlaceholder')}
        value={query}
        onChange={(e) => onQueryChange(e.target.value)}
        style={{ margin: '6px 0 10px' }}
      />

      {query.trim() ? (
        <div>
          <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', padding: '0 6px 6px' }}>
            {t('domainKnowledge.wikiReader.searchResults', { count: searchResults.length })}
          </div>
          {searchResults.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t('domainKnowledge.wikiReader.searchEmpty')}
              style={{ margin: '12px 0' }}
            />
          ) : (
            searchResults
              .slice(0, 50)
              .map((page) => renderEntry(page.path, page.title, page.type))
          )}
        </div>
      ) : outline && outline.topics.length > 0 ? (
        (outline.topics || []).map((topic) => renderNode(topic, 0))
      ) : (
        <div>
          <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', padding: '0 6px 6px' }}>
            <UnorderedListOutlined /> {t('domainKnowledge.wikiReader.browseByCategory')}
          </div>
          {categoryGroups.map(([category, items]) => (
            <div key={category} style={{ marginBottom: 6 }}>
              <div style={{ ...groupTitleStyle, fontSize: 12, cursor: 'default' }}>
                <FileTextOutlined style={{ color: 'var(--yx-text-muted)' }} />
                <span style={{ flex: 1 }}>{category}</span>
                <span style={{ color: '#cbd5e1', fontWeight: 400 }}>{items.length}</span>
              </div>
              <div style={{ paddingLeft: 8 }}>
                {items.slice(0, 100).map((page) => renderEntry(page.path, page.title, page.type))}
              </div>
            </div>
          ))}
        </div>
      )}

      {metaText && (
        <div
          style={{
            marginTop: 12,
            padding: '8px 6px 0',
            borderTop: '1px solid var(--yx-bg-muted)',
            fontSize: 11,
            color: 'var(--yx-text-muted)',
            lineHeight: 1.6,
          }}
        >
          {metaText}
        </div>
      )}
    </div>
  );
}
