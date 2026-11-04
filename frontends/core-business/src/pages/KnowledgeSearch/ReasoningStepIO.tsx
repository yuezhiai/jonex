import React from 'react';
import { useTranslation } from 'react-i18next';
import { Button, message } from 'antd';
import { CaretDownOutlined, CaretRightOutlined, CopyOutlined } from '@ant-design/icons';
import type { ReasoningIOItem, ReasoningStep } from '@/types/knowledgeSearch';

/** 单元格取值：缺失显示「—」（缺失 ≠ 0 / 空字符串），对象折叠为 JSON 文本。 */
function cellText(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'number') return Number.isFinite(v) ? String(v) : '—';
  if (typeof v === 'object') {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  return String(v);
}

function IOText({ item }: { item: ReasoningIOItem }) {
  return <span style={{ wordBreak: 'break-word' }}>{cellText(item.value)}</span>;
}

function IOList({ item }: { item: ReasoningIOItem }) {
  const values = Array.isArray(item.value) ? item.value : [];
  if (values.length === 0) return <span>—</span>;
  return <span style={{ wordBreak: 'break-word' }}>{values.map(cellText).join('、')}</span>;
}

function IOKv({ item }: { item: ReasoningIOItem }) {
  const value = item.value;
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return <span>{cellText(value)}</span>;
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return <span>—</span>;
  return (
    <dl style={{ margin: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: 'flex', gap: 6, fontSize: 12, lineHeight: 1.7 }}>
          <dt style={{ color: '#94a3b8', flexShrink: 0 }}>{k}：</dt>
          <dd style={{ margin: 0, color: '#334155', wordBreak: 'break-word' }}>{cellText(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function IOTable({ item }: { item: ReasoningIOItem }) {
  const { t } = useTranslation();
  const [showAll, setShowAll] = React.useState(false);
  const rows = item.rows || [];
  // 旧数据 / 未知来源可能没有 columns：按首行 key 顺序推断，label 直接用 key。
  const columns = item.columns?.length
    ? item.columns
    : rows.length > 0
      ? Object.keys(rows[0]).map((key) => ({ key, label: key }))
      : [];
  const previewRows = Math.max(1, Number(item.preview_rows ?? 5) || 5);
  const shown = showAll ? rows : rows.slice(0, previewRows);
  const total = item.total ?? rows.length;
  const canExpand = rows.length > previewRows;
  const truncated = Boolean(item.truncated) || total > rows.length;
  if (columns.length === 0 || rows.length === 0) {
    return <span>{t('knowledgeSearch.ioNoRows')}</span>;
  }
  return (
    <div>
      <div style={{ overflowX: 'auto', border: '1px solid #ece9fb', borderRadius: 6, background: '#fff' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ background: '#faf8ff' }}>
              {columns.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  style={{
                    textAlign: 'left',
                    padding: '5px 8px',
                    color: '#7c3aed',
                    fontWeight: 600,
                    whiteSpace: 'nowrap',
                    borderBottom: '1px solid #ece9fb',
                  }}
                >
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, ri) => (
              <tr key={ri} style={{ borderBottom: ri === shown.length - 1 ? 'none' : '1px solid #f3f0fc' }}>
                {columns.map((c) => {
                  const text = cellText(row[c.key]);
                  return (
                    <td
                      key={c.key}
                      title={text}
                      style={{
                        padding: '5px 8px',
                        color: '#475569',
                        maxWidth: 260,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {text}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
        {canExpand && (
          <Button
            type="link"
            size="small"
            style={{ padding: 0, height: 'auto', fontSize: 12 }}
            onClick={() => setShowAll((v) => !v)}
          >
            {showAll
              ? t('knowledgeSearch.ioCollapse')
              : truncated
                ? t('knowledgeSearch.ioExpandAll', { shown: rows.length })
                : t('knowledgeSearch.ioViewAll', { total: rows.length })}
          </Button>
        )}
        {truncated && (
          <span style={{ fontSize: 11, color: '#b6bdc7' }}>
            {t('knowledgeSearch.ioPayloadTruncated', { shown: rows.length, total })}
          </span>
        )}
      </div>
    </div>
  );
}

function IOJson({ item }: { item: ReasoningIOItem }) {
  let text: string;
  try {
    text = JSON.stringify(item.value, null, 2) ?? '—';
  } catch {
    text = String(item.value);
  }
  return (
    <pre
      style={{
        margin: 0,
        padding: '6px 8px',
        background: '#f8fafc',
        border: '1px solid #ece9fb',
        borderRadius: 6,
        fontSize: 11,
        lineHeight: 1.5,
        maxHeight: 220,
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      {text}
    </pre>
  );
}

function IOValue({ item }: { item: ReasoningIOItem }) {
  switch (item.type) {
    case 'list':
      return <IOList item={item} />;
    case 'kv':
      return <IOKv item={item} />;
    case 'table':
      return <IOTable item={item} />;
    case 'text':
      return <IOText item={item} />;
    default:
      // 未知 type 降级成 JSON，至少能看到原始值。
      return <IOJson item={item} />;
  }
}

function IOBlock({ title, items }: { title: string; items?: ReasoningIOItem[] | null }) {
  if (!items || items.length === 0) return null;
  return (
    <section style={{ marginTop: 8 }} aria-label={title}>
      <div style={{ fontSize: 12, fontWeight: 600, color: '#6d28d9', marginBottom: 4 }}>{title}</div>
      <dl
        style={{
          margin: 0,
          background: '#fff',
          border: '1px solid #efeafc',
          borderRadius: 8,
          padding: '8px 10px',
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
        }}
      >
        {items.map((item, idx) => (
          <div
            key={`${item.label}-${idx}`}
            style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12 }}
          >
            <dt style={{ width: 96, flexShrink: 0, color: '#94a3b8' }}>{item.label}</dt>
            <dd style={{ flex: 1, minWidth: 0, margin: 0, color: '#334155' }}>
              <IOValue item={item} />
              {item.note && (
                <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>{item.note}</div>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** 步骤输入 / 输出（状态与耗时已收进步骤头部，不再重复一块「执行详情」）。 */
export function ReasoningStepIO({ step }: { step: ReasoningStep }) {
  const { t } = useTranslation();
  const hasIO = (step.input?.length ?? 0) > 0 || (step.output?.length ?? 0) > 0;
  if (!hasIO) {
    return (
      <div style={{ fontSize: 11, color: '#b6bdc7', marginTop: 8 }}>
        {t('knowledgeSearch.ioLegacyMissing')}
      </div>
    );
  }
  return (
    <>
      <IOBlock title={t('knowledgeSearch.ioInput')} items={step.input} />
      <IOBlock title={t('knowledgeSearch.ioOutput')} items={step.output} />
    </>
  );
}

/** 原始 JSON（输入/输出已脱敏）。只在展开时序列化，避免长链一次性 stringify 大对象。 */
export function ReasoningRawJson({ step }: { step: ReasoningStep }) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [copied, setCopied] = React.useState(false);

  const raw = React.useMemo(() => {
    if (!open) return '';
    try {
      return JSON.stringify(step, null, 2);
    } catch {
      return '';
    }
  }, [open, step]);

  const shown = raw.length > 20000 ? raw.slice(0, 20000) + '\n…' : raw;
  const truncated = raw.length > 20000;

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      message.success(t('knowledgeSearch.ioCopied'));
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      message.warning(t('knowledgeSearch.ioCopyFailed'));
    }
  };

  return (
    <div style={{ marginTop: 8 }}>
      <Button
        type="link"
        size="small"
        style={{ padding: 0, height: 'auto', fontSize: 12 }}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <CaretDownOutlined /> : <CaretRightOutlined />}{' '}
        {t('knowledgeSearch.ioRawJson')}
      </Button>
      {open && (
        <div style={{ marginTop: 4 }}>
          <pre
            style={{
              margin: 0,
              padding: '8px 10px',
              background: '#f8fafc',
              border: '1px solid #ece9fb',
              borderRadius: 6,
              fontSize: 11,
              lineHeight: 1.5,
              maxHeight: 320,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {shown}
          </pre>
          {truncated && (
            <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>
              {t('knowledgeSearch.ioJsonTruncated')}
            </div>
          )}
          <Button size="small" icon={<CopyOutlined />} onClick={onCopy} style={{ marginTop: 4 }}>
            {copied ? t('knowledgeSearch.ioCopied') : t('knowledgeSearch.ioCopy')}
          </Button>
        </div>
      )}
    </div>
  );
}

export default ReasoningStepIO;
