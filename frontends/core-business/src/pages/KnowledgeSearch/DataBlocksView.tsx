import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { DatabaseOutlined } from '@ant-design/icons';
import type { DataBlock, DataBlockCandidate } from '@/types/knowledgeSearch';
import ResultBlocksView from '@/components/ResultBlocks/ResultBlocksView';
import styles from './Citation.module.scss';

/** UUID 形状的数据源名（失败块常拿不到 ② 回包的 source_name，只剩 uuid）。 */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** 块文本里「\n\n候选：\n- …」段有**结构化 candidates**（chip 渲染）时，把文本里对应那段切掉、别重复。
 *  ⚠️ 文本可能含**两段**候选（DI 澄清自带的指标候选 + ② 失败接后的实体候选）：
 *  结构化 candidates 取的是 **② 那段** ⇒ 切**最后**一段，DI 的指标候选与「答不了：…」留在正文。 */
export function splitCandidates(text: string): string {
  const marker = '\n\n候选：\n';
  const i = text.lastIndexOf(marker);
  return i === -1 ? text : text.slice(0, i);
}

/** 候选 chip 文案（用户口径 2026-10-21：中文名优先、原名进括号）：
 *  - 概念候选：有中文 `display_name` ⇒ `焚烧线主数据 · wte_a 生产库（LineMaster）`；
 *    没有/与原名相同 ⇒ 退回原名 + 命中词（原行为）。
 *  - 指标候选：**落点实体中文名**优先（`entity_display_name`，如「集团吨发日汇总」），
 *    指标名与原名进括号 —— 只显示指标名时看不出它落在哪张表上。 */
export function candidateChipLabel(c: DataBlockCandidate): string {
  const source = c.source_name ? ` · ${c.source_name}` : '';
  if (c.kind === 'metric') {
    const primary = c.entity_display_name || c.display_name || c.metric || c.entity_name;
    const detail = [
      c.metric && c.metric !== primary ? `指标：${c.metric}` : '',
      c.entity_name,
    ].filter(Boolean).join(' · ');
    return `${primary}${source}${detail ? `（${detail}）` : ''}`;
  }
  const hasZhName = Boolean(c.display_name && c.display_name !== c.entity_name);
  const head = hasZhName ? (c.display_name as string) : c.entity_name;
  const tail = hasZhName
    ? `（${c.entity_name}）`
    : (c.matched_term && c.matched_term !== c.entity_name ? `（${c.matched_term}）` : '');
  return `${head}${source}${tail}`;
}

/** 源名 → 范围标签（演示库按「生产库 / 汇总库」命名；推断不出就不加标签）。 */
export function candidateScopeHint(sourceName?: string | null): string {
  const s = sourceName || '';
  if (s.includes('汇总')) return '汇总源';
  if (s.includes('生产')) return '生产源';
  return '';
}

/** chip 上的小标签：指标 / 聚合兜底 / 源范围。 */
export function candidateChipTags(c: DataBlockCandidate): string[] {
  const tags: string[] = [];
  if (c.kind === 'metric') tags.push('指标');
  if (c.match_kind === 'aggregate') tags.push('聚合兜底');
  const scope = candidateScopeHint(c.source_name);
  if (scope) tags.push(scope);
  return tags;
}

const MATCH_KIND_TEXT: Record<string, string> = {
  exact: '正名', alias: '别名', attribute: '属性', core: '核心词',
  description: '描述', aggregate: '聚合兜底',
};

/** chip 悬浮提示：原名 / 命中词与方式 / 源（不挤占 chip 文案）。 */
export function candidateChipTitle(c: DataBlockCandidate): string {
  const parts = [`原名：${c.entity_name}`];
  if (c.matched_term) {
    const kind = c.match_kind ? (MATCH_KIND_TEXT[c.match_kind] || c.match_kind) : '';
    parts.push(`命中：${c.matched_term}${kind ? `（${kind}）` : ''}`);
  }
  if (c.source_name) parts.push(`源：${c.source_name}`);
  if (c.kind === 'metric' && c.metric) parts.push(`指标：${c.metric}`);
  parts.push('点选后用「用所选重问」按点名重问');
  return parts.join('；');
}

/** 候选选择（pin 是 `数据源 → 实体` 单值：同一源选一个，跨源可多选）。 */
export function toggleChipSelection(
  prev: Record<string, string>, c: DataBlockCandidate,
): Record<string, string> {
  const ds = c.data_source_id;
  if (!ds) return prev;
  if (prev[ds] === c.entity_name) {
    const next = { ...prev };
    delete next[ds];
    return next;
  }
  return { ...prev, [ds]: c.entity_name };
}

const chipTagStyle = (tag: string): React.CSSProperties => {
  const base: React.CSSProperties = {
    marginLeft: 4, fontSize: 11, lineHeight: '16px', borderRadius: 4, padding: '0 4px',
  };
  if (tag === '聚合兜底') {
    return { ...base, color: '#c2410c', background: '#fff7ed', border: '1px solid #fed7aa' };
  }
  if (tag === '指标') {
    return { ...base, color: '#0e7490', background: '#ecfeff', border: '1px solid #a5f3fc' };
  }
  return { ...base, color: '#475569', background: '#f1f5f9', border: '1px solid #e2e8f0' };
};

/** 候选 chip 列表：多选（跨源）→「用所选重问」一次提交多个 pin。 */
function CandidateChips({ candidates, onPick }: {
  candidates: DataBlockCandidate[];
  onPick?: (pins: Record<string, string>) => void;
}) {
  const [selected, setSelected] = useState<Record<string, string>>({});
  const count = Object.keys(selected).length;
  return (
    <div>
      <div style={{ marginTop: 6, fontSize: 12, color: '#64748b' }}>
        {onPick
          ? '候选（可多选；同一数据源选一个，点「用所选重问」）：'
          : '候选（在提问里点名其中之一再问）：'}
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
        {candidates.map((c, ci) => {
          const clickable = Boolean(onPick && c.data_source_id);
          const isSelected = Boolean(c.data_source_id && selected[c.data_source_id] === c.entity_name);
          const chipStyle: React.CSSProperties = {
            fontSize: 12,
            lineHeight: '20px',
            background: isSelected ? '#dbeafe' : '#eef2ff',
            border: `1px solid ${isSelected ? '#3b82f6' : '#c7d2fe'}`,
            color: isSelected ? '#1d4ed8' : '#3730a3',
            borderRadius: 999,
            padding: '1px 10px',
            cursor: clickable ? 'pointer' : 'default',
          };
          const content = (
            <>
              {isSelected ? '✓ ' : ''}
              {candidateChipLabel(c)}
              {candidateChipTags(c).map((tag) => (
                <span key={tag} style={chipTagStyle(tag)}>{tag}</span>
              ))}
            </>
          );
          return clickable ? (
            <button
              key={ci}
              type="button"
              aria-pressed={isSelected}
              title={candidateChipTitle(c)}
              style={chipStyle}
              onClick={() => setSelected((prev) => toggleChipSelection(prev, c))}
            >
              {content}
            </button>
          ) : (
            <span key={ci} style={chipStyle}>{content}</span>
          );
        })}
      </div>
      {onPick && count > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
          <button
            type="button"
            style={{
              fontSize: 12, padding: '2px 12px', borderRadius: 6,
              border: '1px solid #2563eb', background: '#2563eb', color: '#fff', cursor: 'pointer',
            }}
            onClick={() => onPick(selected)}
          >
            用所选重问（{count} 个源）
          </button>
          <button
            type="button"
            style={{ fontSize: 12, padding: '2px 8px', border: 'none', background: 'transparent', color: '#64748b', cursor: 'pointer' }}
            onClick={() => setSelected({})}
          >
            清空
          </button>
        </div>
      )}
    </div>
  );
}

/** [①②] 数据路径的结构化按源块渲染。
 *
 * - NL→SQL（②）块带 `table` → 渲染**真表格** + 「引擎：NL→SQL（②）」小标；`sql` 折叠可看；
 * - 其余块（DI 文本 / 未接入 / 答不了）→ 原样文本（后端已渲染成人读中文）；
 * - **无 `blocks` 时返回 null** —— 老后端 / 开关关（`NL2SQL_FALLBACK_ENABLED=0`）零回归。
 */
export default function DataBlocksView({ blocks, onPick }: {
  blocks?: DataBlock[] | null;
  /** [①②] 候选 chip 提交：`{数据源 ID: 实体名}`（router 的 `entity_pins`，支持多个源） */
  onPick?: (pins: Record<string, string>) => void;
}) {
  const { t } = useTranslation();
  if (!blocks || blocks.length === 0) return null;
  return (
    <div style={{ marginTop: 10 }}>
      {blocks.map((b, i) => (
        <div
          key={`${b.data_source_id}-${i}`}
          style={{
            border: '1px solid #e2e8f0',
            borderRadius: 8,
            padding: '8px 12px',
            marginTop: 8,
            background: '#f8fafc',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <span className={styles.dataBadge} title={t('knowledgeSearch.dataBadgeHint')}>
              <DatabaseOutlined style={{ fontSize: 10 }} />
              {t('knowledgeSearch.dataBadge')}
            </span>
            <span style={{ fontWeight: 600, color: '#0f172a', fontSize: 13 }}
              title={b.source_name}>
              {UUID_RE.test(b.source_name || '')
                ? t('knowledgeSearch.dataSourceFallback')
                : b.source_name}
            </span>
            {b.engine === 'nl2sql' && (
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
                引擎：NL→SQL（②）
              </span>
            )}
            {b.truncated && <span style={{ fontSize: 12, color: '#b45309' }}>（结果已截断）</span>}
          </div>
          {b.blocks && b.blocks.some((block) =>
            block.type === 'table' || block.type === 'chart') ? (
            <ResultBlocksView blocks={b.blocks} includeText={false} />
          ) : b.engine === 'nl2sql' && b.table && b.table.columns.length > 0 ? (
            <div style={{ overflowX: 'auto' }}>
              <table style={{ borderCollapse: 'collapse', fontSize: 13, color: '#0f172a' }}>
                <thead>
                  <tr>
                    {b.table.columns.map((c, ci) => (
                      <th
                        key={ci}
                        style={{
                          border: '1px solid #e2e8f0',
                          background: '#f1f5f9',
                          padding: '4px 10px',
                          textAlign: 'left',
                          fontWeight: 600,
                        }}
                      >
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {b.table.rows.length === 0 && (
                    <tr>
                      <td
                        colSpan={b.table.columns.length}
                        style={{ border: '1px solid #e2e8f0', padding: '4px 10px', color: '#94a3b8' }}
                      >
                        （0 行）
                      </td>
                    </tr>
                  )}
                  {b.table.rows.map((r, ri) => (
                    <tr key={ri}>
                      {r.map((v, vi) => (
                        <td key={vi} style={{ border: '1px solid #e2e8f0', padding: '4px 10px' }}>
                          {v === null || v === undefined ? '' : String(v)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : b.candidates && b.candidates.length > 0 ? (
            <div>
              <div style={{ whiteSpace: 'pre-wrap', fontSize: 13, color: '#334155', lineHeight: 1.7 }}>
                {splitCandidates(b.text)}
              </div>
              <CandidateChips candidates={b.candidates} onPick={onPick} />
            </div>
          ) : (
            <div style={{ whiteSpace: 'pre-wrap', fontSize: 13, color: '#334155', lineHeight: 1.7 }}>
              {b.text}
            </div>
          )}
          {b.sql && (
            <details style={{ marginTop: 6 }}>
              <summary style={{ cursor: 'pointer', fontSize: 12, color: '#64748b' }}>查看 SQL</summary>
              <pre
                style={{
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                  fontSize: 12,
                  color: '#334155',
                  background: '#f1f5f9',
                  borderRadius: 6,
                  padding: 8,
                  marginTop: 4,
                }}
              >
                {b.sql}
              </pre>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}
