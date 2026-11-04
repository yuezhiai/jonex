import React from 'react';
import { useTranslation } from 'react-i18next';
import { CaretDownOutlined, CaretRightOutlined } from '@ant-design/icons';

/** 后端 `_tool_call_views` 产出的单条调用（推理步骤 `ontology_tools` 的 detail.calls[]） */
export interface OntologyToolCallView {
  name: string;
  label?: string;
  round?: number | null;
  arguments?: Record<string, unknown>;
  ok: boolean;
  facts_added?: number;
  duration_ms?: number;
  error?: string;
  result?: unknown;
  result_truncated?: boolean;
  result_omitted?: boolean;
}

const NS = 'knowledgeSearch.toolCalls';

function pretty(value: unknown): string {
  if (value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** 入参一行摘要：`键=值`，外部工具只有 action_name 与 input_keys */
function argsBrief(args?: Record<string, unknown>): string {
  if (!args) return '';
  return Object.entries(args)
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(',') : typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join('  ');
}

const preStyle: React.CSSProperties = {
  margin: 0,
  padding: '6px 8px',
  background: '#f8fafc',
  border: '1px solid #ece9fb',
  borderRadius: 6,
  fontSize: 11,
  lineHeight: 1.5,
  maxHeight: 260,
  overflow: 'auto',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
};

function CallRow({ call, index }: { call: OntologyToolCallView; index: number }) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const cached = call.ok && call.error === 'cached';
  const external = call.name === 'ontology_call_tool';
  const statusText = !call.ok
    ? t(`${NS}.failed`, { error: call.error || '—' })
    : cached
      ? t(`${NS}.cached`)
      : t(`${NS}.factsAdded`, { count: call.facts_added ?? 0 });
  const statusColor = !call.ok ? '#ef4444' : cached ? '#94a3b8' : '#10b981';
  return (
    <div style={{ borderBottom: '1px dashed #efeafc', padding: '4px 0' }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{
          all: 'unset',
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'baseline',
          gap: 6,
          flexWrap: 'wrap',
          fontSize: 12,
          width: '100%',
        }}
      >
        <span style={{ color: '#94a3b8' }}>{open ? <CaretDownOutlined /> : <CaretRightOutlined />}</span>
        <span style={{ color: '#94a3b8', minWidth: 18 }}>{index}.</span>
        <span
          style={{
            fontSize: 11,
            background: external ? '#ecfeff' : '#f5f3ff',
            color: external ? '#0e7490' : '#7c3aed',
            borderRadius: 4,
            padding: '1px 7px',
            whiteSpace: 'nowrap',
          }}
        >
          {call.label || call.name}
        </span>
        <span style={{ color: '#4c1d95', wordBreak: 'break-all' }}>{argsBrief(call.arguments)}</span>
        <span style={{ color: statusColor, fontSize: 11 }}>{statusText}</span>
        {call.duration_ms != null && (
          <span style={{ color: '#b6bdc7', fontSize: 11 }}>{call.duration_ms} ms</span>
        )}
      </button>
      {open && (
        <div style={{ margin: '6px 0 4px 24px', display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ fontSize: 11, color: '#94a3b8' }}>
            {t(`${NS}.toolName`)}：<code>{call.name}</code>
          </div>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: '#6d28d9', marginBottom: 2 }}>{t(`${NS}.input`)}</div>
            <pre style={preStyle}>{pretty(call.arguments ?? {})}</pre>
            {external && <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>{t(`${NS}.inputRedacted`)}</div>}
          </div>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: '#6d28d9', marginBottom: 2 }}>{t(`${NS}.output`)}</div>
            {call.result_omitted ? (
              <div style={{ fontSize: 11, color: '#94a3b8' }}>{t(`${NS}.resultOmitted`)}</div>
            ) : call.result === undefined ? (
              <div style={{ fontSize: 11, color: '#94a3b8' }}>—</div>
            ) : (
              <>
                <pre style={preStyle}>{pretty(call.result)}</pre>
                {call.result_truncated && (
                  <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>{t(`${NS}.resultTruncated`)}</div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** 推理步骤「本体工具调用」的正文：按轮次列出模型发出的每次工具调用，单条可展开看入参与返回。 */
export default function OntologyToolCallsView({
  calls,
  total,
  rounds,
}: {
  calls: OntologyToolCallView[];
  total?: number;
  rounds?: number | null;
}) {
  const { t } = useTranslation();
  if (calls.length === 0) return null;
  // 按轮次分组（旧快照没有 round 时整体当一组）
  const groups: Array<{ round: number | null; items: Array<{ call: OntologyToolCallView; index: number }> }> = [];
  calls.forEach((call, i) => {
    const r = call.round ?? null;
    const last = groups[groups.length - 1];
    if (last && last.round === r) last.items.push({ call, index: i + 1 });
    else groups.push({ round: r, items: [{ call, index: i + 1 }] });
  });
  return (
    <div>
      <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
        {t(`${NS}.summary`, { rounds: rounds ?? '—', count: total ?? calls.length })}
      </div>
      {groups.map((g, gi) => (
        <div key={gi} style={{ marginBottom: 6 }}>
          {g.round != null && (
            <div style={{ fontSize: 11, color: '#7c3aed', fontWeight: 600, margin: '2px 0' }}>
              {t(`${NS}.round`, { round: g.round })}
            </div>
          )}
          {g.items.map(({ call, index }) => (
            <CallRow key={index} call={call} index={index} />
          ))}
        </div>
      ))}
      {total != null && total > calls.length && (
        <div style={{ fontSize: 11, color: '#94a3b8' }}>{t(`${NS}.truncated`, { shown: calls.length, total })}</div>
      )}
    </div>
  );
}
