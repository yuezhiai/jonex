import React from 'react';
import { Descriptions, Empty, Tag } from 'antd';
import { useTranslation } from 'react-i18next';
import type { RunBundle, SmokeResult } from '../../../api/evolution';
import { TD, TH } from './tableCells';

export const smokeStatusColor = (status: string): string => {
  if (status === 'passed') return 'success';
  if (status === 'failed') return 'error';
  if (status === 'warn') return 'warning';
  return 'default';
};

export const outcomeColor = (outcome?: string | null): string => {
  if (outcome === 'pass') return 'success';
  if (outcome === 'fail') return 'error';
  if (outcome === 'error') return 'warning';
  return 'default';
};

interface SmokeCardProps {
  /** 采纳后 smoke 结果（新形状或旧 `{case: outcome}` 映射）。 */
  smoke: RunBundle['smoke'];
  /** run 行上的 smoke_status：`warn` 时即使 smoke.status=failed 也要显式提示（C4-2）。 */
  runSmokeStatus?: string | null;
}

/** 采纳后 smoke 卡：新形状（status/mode/reasons/cases）与旧 `{case: outcome}` 映射都要能渲染。 */
export default function SmokeCard({ smoke, runSmokeStatus }: SmokeCardProps) {
  const { t } = useTranslation();
  if (!smoke) {
    return (
      <div className="yx-card" style={{ marginBottom: 16 }}>
        <h3>{t('evolutionConsole.smokeTitle')}</h3>
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('evolutionConsole.smokeNotRun')} />
      </div>
    );
  }

  const result = smoke as SmokeResult;
  const isNewShape = Array.isArray(result.cases) || typeof result.status === 'string';
  if (!isNewShape) {
    return (
      <div className="yx-card" style={{ marginBottom: 16 }}>
        <h3>{t('evolutionConsole.smokeTitle')}</h3>
        <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
          {t('evolutionConsole.smokeLegacyHint')}
        </p>
        <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
          {Object.entries(smoke as Record<string, string>).map(([caseId, outcome]) => (
            <li key={caseId}>
              <code>{caseId}</code>：
              <Tag color={outcomeColor(outcome)}>
                {t(`evolutionConsole.smoke.${outcome}`, { defaultValue: outcome })}
              </Tag>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  const status = result.status || 'skipped';
  const warnMode = runSmokeStatus === 'warn';
  // warn 模式：run 行记的是 `warn`（enforce 模式下 failed 会真失败），
  // 卡片不能只显示底层 smoke.status=failed，否则用户看不到「已放行但有告警」。
  const displayStatus = warnMode ? 'warn' : status;
  const mode = result.mode || '-';
  const reasons = result.reasons ?? [];
  const cases = result.cases ?? [];

  return (
    <div className="yx-card" style={{ marginBottom: 16 }}>
      <h3>{t('evolutionConsole.smokeTitle')}</h3>
      <Descriptions size="small" column={3} style={{ marginBottom: 8 }}>
        <Descriptions.Item label={t('evolutionConsole.smokeStatusLabel')}>
          <Tag color={smokeStatusColor(displayStatus)}>
            {t(`evolutionConsole.smokeStatus.${displayStatus}`, { defaultValue: displayStatus })}
            <span style={{ marginLeft: 6, fontSize: 12, opacity: 0.65 }}>{displayStatus}</span>
          </Tag>
          {warnMode && status !== 'warn' && (
            <span style={{ color: '#8c8c8c', fontSize: 12, marginLeft: 6 }}>
              （{t('evolutionConsole.smokeStatus.warn')}：{status}）
            </span>
          )}
        </Descriptions.Item>
        <Descriptions.Item label={t('evolutionConsole.smokeMode')}>
          {t(`evolutionConsole.smokeModeLabel.${mode}`, { defaultValue: mode })}
        </Descriptions.Item>
        <Descriptions.Item label={t('evolutionConsole.smokeDuration')}>
          {typeof result.duration_ms === 'number' ? `${result.duration_ms} ms` : '-'}
        </Descriptions.Item>
      </Descriptions>

      {reasons.length > 0 && (
        <>
          <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.smokeReasons')}</h4>
          <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
            {reasons.map((reason, i) => (
              <li key={`${reason.case_id ?? '-'}-${reason.reason ?? i}`}>
                <code>{reason.case_id ?? '-'}</code>：
                {reason.reason
                  ? t(`evolutionConsole.smokeReason.${reason.reason}`, { defaultValue: reason.reason })
                  : '-'}
              </li>
            ))}
          </ul>
        </>
      )}

      {cases.length > 0 ? (
        <>
          <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.smokeCases')}</h4>
          <table style={{ width: '100%', fontSize: 13 }}>
            <thead>
              <tr>
                <TH>{t('evolutionConsole.smokeCase')}</TH>
                <TH>{t('evolutionConsole.smokeKind')}</TH>
                <TH>{t('evolutionConsole.smokeQuestion')}</TH>
                <TH>{t('evolutionConsole.smokeOutcome')}</TH>
                <TH>{t('evolutionConsole.smokeReasonCode')}</TH>
              </tr>
            </thead>
            <tbody>
              {cases.map((item, i) => (
                <tr key={item.case_id ?? i}>
                  <TD><code>{item.case_id ?? '-'}</code></TD>
                  <TD>{item.kind ?? '-'}</TD>
                  <TD>{item.question ?? '-'}</TD>
                  <TD>
                    <Tag color={outcomeColor(item.outcome)}>
                      {t(`evolutionConsole.smoke.${item.outcome}`, { defaultValue: item.outcome ?? '-' })}
                    </Tag>
                  </TD>
                  <TD>{item.reason_code ?? '-'}</TD>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('evolutionConsole.smokeCasesEmpty')} />
      )}
    </div>
  );
}
