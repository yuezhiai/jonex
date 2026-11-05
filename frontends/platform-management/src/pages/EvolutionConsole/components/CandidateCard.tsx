import React from 'react';
import { Descriptions, Space, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import type { CandidateItem } from '../../../api/evolution';
import SqlPre from './SqlPre';
import { TD, TH } from './tableCells';

const jsonText = (value: unknown): string => {
  try {
    const text = JSON.stringify(value, null, 2);
    return text === undefined ? '-' : text;
  } catch {
    return String(value);
  }
};

/** 候选 target：alias 批是 `[{concept, alias}]`，query_rule 批是 `[{target_code}]`。 */
const targetLabel = (target: unknown): string => {
  if (typeof target === 'string') return target;
  if (Array.isArray(target)) {
    if (target.length === 0) return '-';
    return target.map((item) => {
      if (item && typeof item === 'object') {
        const obj = item as Record<string, unknown>;
        if (obj.concept !== undefined) return `${String(obj.concept)}:${String(obj.alias ?? '')}`;
        if (obj.target_code !== undefined) return String(obj.target_code);
      }
      return jsonText(item);
    }).join(', ');
  }
  if (target && typeof target === 'object') return jsonText(target);
  return '-';
};

/** 单条候选卡：层/类型/靶点/期望变更/理由/证据（问句与 SQL）/before-after 折叠。 */
export default function CandidateCard({ candidate }: { candidate: CandidateItem }) {
  const { t } = useTranslation();
  const evidence = candidate.evidence;
  const observations = evidence?.observations ?? [];
  const adhocCases = evidence?.adhoc_cases ?? [];
  const changes = candidate.expected_changes ?? [];

  return (
    <div className="yx-card">
      <Space wrap size={8} style={{ marginBottom: 8 }}>
        <b>#{candidate.candidate_no}</b>
        {candidate.layer && (
          <Tag>
            {t('evolutionConsole.candidateLayer')}：{candidate.layer}
          </Tag>
        )}
        {candidate.edit_type && <Tag color="blue">{candidate.edit_type}</Tag>}
        {candidate.status && <Tag>{candidate.status}</Tag>}
        {candidate.gate_decision && (
          <Tag color={candidate.gate_decision === 'accept' ? 'success' : candidate.gate_decision === 'reject' ? 'error' : 'warning'}>
            {candidate.gate_decision}
          </Tag>
        )}
      </Space>

      <Descriptions size="small" column={1}>
        <Descriptions.Item label={t('evolutionConsole.candidateTarget')}>
          <code>{targetLabel(candidate.target)}</code>
        </Descriptions.Item>
      </Descriptions>

      {changes.length > 0 && (
        <>
          <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.candidateExpectedChanges')}</h4>
          <table style={{ fontSize: 13, marginBottom: 8 }}>
            <thead>
              <tr>
                <TH>{t('evolutionConsole.smokeCase')}</TH>
                <TH>{t('evolutionConsole.candidateFrom')}</TH>
                <TH>{t('evolutionConsole.candidateTo')}</TH>
              </tr>
            </thead>
            <tbody>
              {changes.map((change) => (
                <tr key={change.case_id}>
                  <TD><code>{change.case_id}</code></TD>
                  <TD>{change.from ?? '-'}</TD>
                  <TD>{change.to ?? '-'}</TD>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {candidate.rationale && (
        <p style={{ whiteSpace: 'pre-wrap', margin: '4px 0' }}>{candidate.rationale}</p>
      )}

      {observations.length > 0 && (
        <>
          <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.candidateObservations')}</h4>
          <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
            {observations.map((obs, i) => (
              <li key={obs.id ?? i} style={{ marginBottom: 6 }}>
                <div>{obs.question ?? '-'}</div>
                <div style={{ color: '#8c8c8c', fontSize: 12 }}>
                  {obs.reason_code ?? '-'}
                  {obs.row_count !== undefined && obs.row_count !== null
                    ? ` · rows=${obs.row_count}` : ''}
                </div>
                {obs.sql && (
                  <details>
                    <summary style={{ cursor: 'pointer' }}>SQL</summary>
                    <SqlPre text={obs.sql} maxHeight={200} />
                  </details>
                )}
                {obs.rejected_sql && (
                  <details>
                    <summary style={{ cursor: 'pointer' }}>{t('evolutionConsole.detailRejectedSql')}</summary>
                    <SqlPre text={obs.rejected_sql} maxHeight={200} />
                  </details>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {adhocCases.length > 0 && (
        <>
          <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.candidateAdhoc')}</h4>
          <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
            {adhocCases.map((item) => (
              <li key={item.case_id} style={{ marginBottom: 6 }}>
                <Space size={4} wrap>
                  <code>{item.case_id}</code>
                  {item.verification && <Tag>{item.verification}</Tag>}
                </Space>
                <div>{item.question ?? '-'}</div>
                {item.failure_signature ? (
                  <details>
                    <summary style={{ cursor: 'pointer' }}>{t('evolutionConsole.candidateFailureSignature')}</summary>
                    <SqlPre text={jsonText(item.failure_signature)} maxHeight={160} />
                  </details>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      )}

      <details style={{ marginTop: 8 }}>
        <summary style={{ cursor: 'pointer' }}>{t('evolutionConsole.candidateBeforeAfter')}</summary>
        <SqlPre
          text={jsonText({ before: candidate.before ?? null, after: candidate.after ?? null })}
          maxHeight={320}
        />
      </details>
      {candidate.gate_reasons !== undefined && candidate.gate_reasons !== null && (
        <details style={{ marginTop: 4 }}>
          <summary style={{ cursor: 'pointer' }}>{t('evolutionConsole.candidateGateReasons')}</summary>
          <SqlPre text={jsonText(candidate.gate_reasons)} maxHeight={200} />
        </details>
      )}
    </div>
  );
}
