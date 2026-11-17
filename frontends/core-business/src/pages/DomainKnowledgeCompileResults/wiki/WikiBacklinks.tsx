/**
 * [jonex] 「链入本页」（反向链接 / What links here）。
 *
 * 维基百科的 "What links here" 是读者判断「这一页在整本书里的位置」的主要依据：
 * 一个概念被谁引用，往往比它自己的正文更能说明它的角色。
 *
 * 两种来源在后端合并、注解在 kind 上：
 *   link    = 引擎 frontmatter 页间链接（编译期建立的强关系）
 *   mention = 正文首次提及（与正文蓝链同一批目标、同一套门槛）
 * 无链入时给出明确空态——真实语料里大部分页面确实没有被链入，这不是错误。
 */
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Skeleton, Tag } from 'antd';
import { ImportOutlined } from '@ant-design/icons';
import type { WikiBacklinkResult } from '@/types/domainKnowledge';

/** 首屏最多显示这么多条，其余折叠（避免长列表把正文推得太远） */
const COLLAPSED_COUNT = 8;

interface WikiBacklinksProps {
  result: WikiBacklinkResult | null;
  loading?: boolean;
  onOpenPage: (path: string) => void;
}

export default function WikiBacklinks({ result, loading, onOpenPage }: WikiBacklinksProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  const items = result?.items || [];
  const visible = expanded ? items : items.slice(0, COLLAPSED_COUNT);

  return (
    <div style={{ marginTop: 26 }}>
      <div style={{ fontSize: 16, fontWeight: 600, color: 'var(--yx-brand-dark)', marginBottom: 8 }}>
        <ImportOutlined /> {t('domainKnowledge.wikiReader.backlinks')}
        {items.length > 0 && (
          <span style={{ fontSize: 12, color: 'var(--yx-text-muted)', fontWeight: 400, marginLeft: 8 }}>
            {t('domainKnowledge.wikiReader.backlinksCount', { count: result?.total || items.length })}
            {result && result.mentionCount > 0
              ? ` · ${t('domainKnowledge.wikiReader.backlinksMention', { count: result.mentionCount })}`
              : ''}
          </span>
        )}
      </div>
      {loading && items.length === 0 ? (
        <Skeleton active paragraph={{ rows: 1 }} title={false} />
      ) : items.length === 0 ? (
        <div style={{ fontSize: 13, color: 'var(--yx-text-muted)' }}>{t('domainKnowledge.wikiReader.noBacklinks')}</div>
      ) : (
        <>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {visible.map((item) => (
              <a
                key={`${item.path}-${item.kind}`}
                onClick={() => onOpenPage(item.path)}
                style={{
                  color: '#2563eb',
                  fontSize: 13,
                  padding: '2px 10px',
                  background: 'var(--yx-bg-subtle)',
                  borderRadius: 14,
                  border: '1px solid var(--yx-border)',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                }}
              >
                {item.title}
                <Tag
                  bordered={false}
                  color={item.kind.includes('link') ? 'blue' : 'purple'}
                  style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: '16px' }}
                >
                  {item.kind.includes('link')
                    ? t('domainKnowledge.wikiReader.backlinkKindLink')
                    : t('domainKnowledge.wikiReader.backlinkKindMention')}
                </Tag>
              </a>
            ))}
          </div>
          {items.length > COLLAPSED_COUNT && (
            <a
              style={{ fontSize: 12, color: '#2563eb', display: 'inline-block', marginTop: 8 }}
              onClick={() => setExpanded((prev) => !prev)}
            >
              {expanded
                ? t('domainKnowledge.wikiReader.backlinksCollapse')
                : t('domainKnowledge.wikiReader.backlinksExpand', {
                    count: items.length - COLLAPSED_COUNT,
                  })}
            </a>
          )}
        </>
      )}
    </div>
  );
}
