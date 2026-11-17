/**
 * [jonex] 条目页右栏的「本页关联」分组列表。
 *
 * 为什么是列表不是图：右栏只有 240px，10 个中文标题 + 连线一定压叠，画图必丑；
 * 而读者在这里要的是「去哪」，列表的扫读效率更高。图留给图谱 Tab（有空间），
 * 两处共用同一个分组模型（wikiLinkGroups.ts）。
 */
import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { WikiLinkGroup, WikiLinkItem } from './wikiRelations';
import { LINK_KIND_COLOR, LINK_KIND_KEY } from './wikiLinkStyle';

interface WikiLinkGroupsProps {
  groups: WikiLinkGroup[];
  onOpenPage: (path: string) => void;
  onOpenDocument?: (documentId: string) => void;
  /** 每个分组默认展示条数（超出给「展开全部」） */
  maxPerGroup?: number;
  /** 右栏紧凑模式（默认），标题字号与间距更小 */
  compact?: boolean;
}

function GroupRows({
  items,
  onOpen,
  max,
}: {
  items: WikiLinkItem[];
  onOpen: (item: WikiLinkItem) => void;
  max: number;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? items : items.slice(0, max);

  return (
    <>
      {shown.map((item) => (
        <div
          key={item.key}
          role="button"
          tabIndex={0}
          onClick={() => onOpen(item)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onOpen(item);
          }}
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 6,
            padding: '5px 8px',
            marginLeft: -8,
            borderRadius: 6,
            cursor: 'pointer',
            transition: 'background 0.15s',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = '#f4f8ff';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'transparent';
          }}
        >
          <span
            style={{
              width: 2,
              alignSelf: 'stretch',
              minHeight: 14,
              borderRadius: 1,
              background: LINK_KIND_COLOR[item.kind],
              opacity: 0.75,
              marginTop: 2,
              flex: '0 0 auto',
            }}
          />
          <span style={{ minWidth: 0, flex: '1 1 auto' }}>
            <span
              style={{
                display: 'block',
                fontSize: 12,
                lineHeight: '16px',
                color: '#18365f',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
              title={item.title}
            >
              {item.title}
            </span>
            {item.note ? (
              <span
                style={{
                  display: 'block',
                  fontSize: 11,
                  lineHeight: '15px',
                  color: 'var(--yx-text-muted)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={item.note}
              >
                {item.note}
              </span>
            ) : null}
          </span>
        </div>
      ))}
      {items.length > max ? (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          style={{
            border: 'none',
            background: 'transparent',
            color: '#2f72df',
            fontSize: 11,
            cursor: 'pointer',
            padding: '2px 0 0',
          }}
        >
          {expanded
            ? t('domainKnowledge.wikiReader.collapse')
            : t('domainKnowledge.wikiReader.expandMore', { count: items.length - max })}
        </button>
      ) : null}
    </>
  );
}

export default function WikiLinkGroups({
  groups,
  onOpenPage,
  onOpenDocument,
  maxPerGroup = 8,
  compact = true,
}: WikiLinkGroupsProps) {
  const { t } = useTranslation();
  const total = useMemo(() => groups.reduce((sum, g) => sum + g.items.length, 0), [groups]);
  if (!total) return null;

  const open = (item: WikiLinkItem) => {
    if (item.kind === 'source') onOpenDocument?.(item.path);
    else onOpenPage(item.path);
  };

  return (
    <div
      style={{
        border: '1px solid #e5ebf3',
        borderRadius: 10,
        background: '#fff',
        padding: compact ? '10px 12px 12px' : '14px 16px 16px',
        boxShadow: '0 1px 3px rgba(39,72,116,0.04)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: '#18365f' }}>
          {t('domainKnowledge.wikiReader.linkGroupsTitle')}
        </span>
        <span
          style={{
            fontSize: 10,
            lineHeight: '16px',
            color: '#2f72df',
            background: '#eaf2ff',
            borderRadius: 8,
            padding: '0 6px',
          }}
        >
          {total}
        </span>
      </div>
      {groups.map((group) => (
        <div key={group.kind} style={{ marginBottom: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 2 }}>
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: LINK_KIND_COLOR[group.kind],
                flex: '0 0 auto',
              }}
            />
            <span style={{ fontSize: 11, color: '#5b708c', fontWeight: 600 }}>{t(LINK_KIND_KEY[group.kind])}</span>
            <span style={{ fontSize: 10, color: '#9fb0c6' }}>{group.items.length}</span>
          </div>
          <GroupRows items={group.items} onOpen={open} max={maxPerGroup} />
        </div>
      ))}
    </div>
  );
}
