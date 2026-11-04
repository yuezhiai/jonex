import React, { useState } from 'react';
import { CaretDownOutlined, CaretRightOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { DataLineageBlock, DataLineageEndpoint } from '@/types/knowledgeSearch';
import { hasLineage, lineageSummary } from './dataLineageModel';

const NS = 'domainKnowledge.search.dataLineage';

/** Backend origin value -> i18n key. Unknown origins fall back to the raw value. */
const ORIGIN_KEYS: Record<string, string> = {
  manual: `${NS}.manual`,
  platform: `${NS}.platform`,
  declared: `${NS}.declared`,
  comment: `${NS}.comment`,
  value: `${NS}.value`,
  derived: `${NS}.derived`,
};

const originTagStyle: React.CSSProperties = {
  marginLeft: 6,
  fontSize: 11,
  lineHeight: '18px',
  color: '#3730a3',
  background: '#eef2ff',
  border: '1px solid #c7d2fe',
  borderRadius: 4,
  padding: '0 5px',
};

function endpointText(e: DataLineageEndpoint): string {
  return e.column ? `${e.endpoint}.${e.column}` : e.endpoint;
}

/** [A] Lineage explanation block attached to ontology answers.
 *
 * Returns `null` when the block is absent or has no items, so old backends and
 * non-ontology search paths render exactly as before. `unknown` is rendered as
 * an explicit note (never as "none"); `truncated` is surfaced when true.
 */
export default function DataLineageView({ block }: { block?: DataLineageBlock | null }) {
  const { t } = useTranslation();
  // 与「数据引用」同款折叠块：默认收起，位于数据引用下方。
  const [open, setOpen] = useState(false);
  if (!hasLineage(block)) return null;
  const items = lineageSummary(block);

  const renderEndpoints = (label: string, endpoints: DataLineageEndpoint[]) => (
    <div style={{ minWidth: 180 }}>
      <div style={{ fontSize: 12, color: '#64748b' }}>{label}</div>
      {endpoints.length === 0 ? (
        <div style={{ fontSize: 12, color: '#94a3b8' }}>—</div>
      ) : (
        <ul style={{ margin: '4px 0 0', paddingLeft: 16, fontSize: 13, color: '#334155' }}>
          {endpoints.map((e, i) => {
            const originKey = e.origin ? ORIGIN_KEYS[e.origin] : undefined;
            return (
              <li key={`${e.endpoint}-${e.column ?? ''}-${i}`} style={{ lineHeight: 1.8 }}>
                <span style={{ fontFamily: 'monospace' }}>{endpointText(e)}</span>
                {e.origin && (
                  <span style={originTagStyle}>{originKey ? t(originKey) : e.origin}</span>
                )}
                {typeof e.confidence === 'number' && (
                  <span style={{ marginLeft: 6, fontSize: 11, color: '#94a3b8' }}>
                    {e.confidence.toFixed(2)}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );

  return (
    <div
      style={{
        background: '#f8fafc',
        borderLeft: '3px solid #0ea5e9',
        borderRadius: '0 8px 8px 0',
        padding: '14px 18px',
        marginBottom: 16,
      }}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        style={{
          width: '100%',
          padding: 0,
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          justifyContent: 'flex-start',
          fontSize: 12,
          fontWeight: 600,
          color: '#0284c7',
          height: 'auto',
          lineHeight: 'inherit',
          border: 'none',
          background: 'transparent',
          cursor: 'pointer',
        }}
      >
        {open ? <CaretDownOutlined /> : <CaretRightOutlined />}
        {t(`${NS}.title`)}
        {block?.truncated && (
          <span style={{ fontSize: 12, color: '#b45309' }}>{t(`${NS}.truncated`)}</span>
        )}
      </button>
      <div style={{ marginTop: 12 }}>
        {open && items.map((item, i) => {
        const raw = (block as DataLineageBlock).items[i];
        return (
          <div
            key={`${item.dataSourceId}-${item.table}-${i}`}
            style={{ marginTop: 8, borderTop: i === 0 ? 'none' : '1px dashed #e2e8f0', paddingTop: i === 0 ? 0 : 8 }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span style={{ fontWeight: 600, color: '#0f172a', fontSize: 13 }}>{item.table}</span>
              {item.entityType && (
                <span
                  style={{
                    fontSize: 12,
                    lineHeight: '18px',
                    color: '#0369a1',
                    border: '1px solid #bae6fd',
                    background: '#e0f2fe',
                    borderRadius: 4,
                    padding: '0 6px',
                  }}
                >
                  {item.entityType}
                </span>
              )}
              {item.dataSourceId && (
                <span style={{ fontSize: 12, color: '#64748b' }}>{item.dataSourceId}</span>
              )}
            </div>
            {item.unknown && (
              <div style={{ marginTop: 4, fontSize: 12, color: '#b45309' }}>{t(`${NS}.unknown`)}</div>
            )}
            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginTop: 4 }}>
              {renderEndpoints(t(`${NS}.upstream`), raw.upstream ?? [])}
              {renderEndpoints(t(`${NS}.downstream`), raw.downstream ?? [])}
            </div>
          </div>
        );
      })}
      </div>
    </div>
  );
}
