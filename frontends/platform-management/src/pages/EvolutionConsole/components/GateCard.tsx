import React from 'react';
import { Alert, Button, Descriptions, Popover, Space, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import type { GateDetail, GateTarget, OverrideInfo, TierSlot } from '../../../api/evolution';

const TH = ({ children }: { children: React.ReactNode }) => (
  <th style={{ textAlign: 'left', padding: '2px 8px', color: '#8c8c8c', fontWeight: 400 }}>{children}</th>
);

interface TierRow {
  tier: string;
  parent: TierSlot;
  candidate: TierSlot;
}

const fmt = (s?: TierSlot) => (s ? `${s.pass}/${s.fail}/${s.error}` : '-');

const decisionColorOf = (value: string | null | undefined): string =>
  value === 'accept' ? 'success' : value === 'reject' ? 'error' : value === 'uncertain' ? 'warning' : 'default';

interface GateCardProps {
  gate: GateDetail | null;
  /** `(gate.decision, gate_adhoc)` 合成的有效结论（信任闭环 P0）。 */
  gateEffective?: string | null;
  /** 补 truth 后 ad-hoc 复跑结论。 */
  gateAdhoc?: string | null;
  /** 人工 override 记录；缺失时该行显示 '-'。 */
  override?: OverrideInfo | null;
}

/** 门结论卡：decision / Δ / 配对 / by_tier / new_red / new_error / 目标题 / 抖动面 / 探针 / reasons，
 * 以及信任闭环的 gate_effective / gate_adhoc / override。
 * 形状取自 tests/eval/nl2sql/gate.py::decide（decision ∈ accept/reject/uncertain）。 */
export default function GateCard({ gate, gateEffective, gateAdhoc, override }: GateCardProps) {
  const { t } = useTranslation();

  const trustLoop = (
    <Descriptions size="small" column={3} style={{ marginBottom: 8 }}>
      <Descriptions.Item label={t('evolutionConsole.gateEffective')}>
        {gateEffective ? (
          <Tag color={decisionColorOf(gateEffective)}>
            {t(`evolutionConsole.decision.${gateEffective}`, { defaultValue: gateEffective })}
            <span style={{ marginLeft: 6, fontSize: 12, opacity: 0.65 }}>{gateEffective}</span>
          </Tag>
        ) : '-'}
      </Descriptions.Item>
      <Descriptions.Item label={t('evolutionConsole.gateAdhoc')}>
        {gateAdhoc ? (
          <Tag color={decisionColorOf(gateAdhoc)}>
            {t(`evolutionConsole.decision.${gateAdhoc}`, { defaultValue: gateAdhoc })}
            <span style={{ marginLeft: 6, fontSize: 12, opacity: 0.65 }}>{gateAdhoc}</span>
          </Tag>
        ) : '-'}
      </Descriptions.Item>
      <Descriptions.Item label={t('evolutionConsole.gateOverride')}>
        {override?.used ? (
          <Space size={4} wrap>
            <Tag color="warning">{t('evolutionConsole.gateOverrideUsed')}</Tag>
            <span style={{ color: '#8c8c8c', fontSize: 12 }}>
              {t('evolutionConsole.gateOverrideBy')}：{override.by || '-'}
            </span>
            <Tooltip title={override.reason || ''}>
              <span style={{
                color: '#8c8c8c', fontSize: 12, maxWidth: 200, display: 'inline-block',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>
                {t('evolutionConsole.gateOverrideReason')}：{override.reason || '-'}
              </span>
            </Tooltip>
          </Space>
        ) : '-'}
      </Descriptions.Item>
    </Descriptions>
  );

  if (!gate) {
    return (
      <div>
        <Alert type="info" showIcon message={t('evolutionConsole.gateNone')} />
        {trustLoop}
      </div>
    );
  }

  const decisionColor =
    gate.decision === 'accept' ? 'success' : gate.decision === 'uncertain' ? 'warning' : 'error';
  const tierRows: TierRow[] = Object.entries(gate.by_tier ?? {}).map(([tier, v]) => ({
    tier,
    parent: v.parent,
    candidate: v.candidate,
  }));
  const passes = gate.passes ?? [];
  const totalCases = tierRows.reduce(
    (acc, r) =>
      acc + (r.parent?.pass ?? 0) + (r.parent?.fail ?? 0) + (r.parent?.error ?? 0),
    0);
  const tierColumns: ColumnsType<TierRow> = [
    {
      title: t('evolutionConsole.gateTier'), dataIndex: 'tier',
      render: (v: string) => (v ? (
        <Space size={4}>
          <span>{t(`evolutionConsole.tier.${v}`, { defaultValue: v })}</span>
          <span style={{ color: '#8c8c8c', fontSize: 12 }}>{v}</span>
        </Space>
      ) : t('evolutionConsole.gateUngrouped')),
    },
    { title: t('evolutionConsole.gateParentCol'), dataIndex: 'parent', render: fmt },
    { title: t('evolutionConsole.gateCandidateCol'), dataIndex: 'candidate', render: fmt },
  ];
  const probeRows = Object.entries(gate.probe ?? {});
  const jitter = gate.jitter ?? {};

  return (
    <div>
      <div style={{ marginBottom: 4, textAlign: 'right' }}>
        <Popover placement="left"
                 title={t('evolutionConsole.gateHowToRead')}
                 content={<div style={{ maxWidth: 420, whiteSpace: 'pre-line' }}>
                   {t('evolutionConsole.gateHowToReadBody')}
                 </div>}>
          <Button type="link" size="small" style={{ padding: 0 }}>
            {t('evolutionConsole.gateHowToRead')}
          </Button>
        </Popover>
      </div>
      <Descriptions size="small" column={3} style={{ marginBottom: 8 }}>
        <Descriptions.Item label={t('evolutionConsole.gateDecision')}>
          <Tooltip title={t(`evolutionConsole.decisionTip.${gate.decision}`, { defaultValue: '' })}>
            <Tag color={decisionColor} style={{ fontSize: 14, padding: '2px 10px' }}>
              {t(`evolutionConsole.decision.${gate.decision}`, { defaultValue: gate.decision })}
              <span style={{ marginLeft: 6, fontSize: 12, opacity: 0.65 }}>{gate.decision}</span>
            </Tag>
          </Tooltip>
        </Descriptions.Item>
        <Descriptions.Item label={t('evolutionConsole.gatePasses')}>
          {passes.length === 2 ? (
            <Space size={6}>
              <span style={{ fontSize: 16 }}>{passes[0]} → {passes[1]}</span>
              {totalCases > 0 && (
                <span style={{ color: '#8c8c8c' }}>
                  {t('evolutionConsole.gatePassesTotal', { n: totalCases })}
                </span>
              )}
            </Space>
          ) : '—'}
        </Descriptions.Item>
        <Descriptions.Item label={t('evolutionConsole.gateDelta')}>
          {gate.delta > 0 ? `+${gate.delta}` : String(gate.delta)}
        </Descriptions.Item>
      </Descriptions>

      <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.gateTrustLoop')}</h4>
      {trustLoop}

      {(gate.new_red?.length ?? 0) > 0 && (
        <Alert type="error" showIcon style={{ marginBottom: 8 }}
               message={`${t('evolutionConsole.gateNewRed')}：${gate.new_red?.join(', ')}`} />
      )}
      {(gate.new_error?.length ?? 0) > 0 && (
        <Alert type="error" showIcon style={{ marginBottom: 8 }}
               message={`${t('evolutionConsole.gateNewError')}：${gate.new_error?.join(', ')}`} />
      )}

      <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.gateByTier')}</h4>
      <Table<TierRow> rowKey="tier" size="small" pagination={false}
                      columns={tierColumns} dataSource={tierRows} style={{ marginBottom: 8 }} />

      {(gate.targets?.length ?? 0) > 0 && (
        <>
          <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.gateTargets')}</h4>
          <table style={{ width: '100%', fontSize: 13, marginBottom: 8 }}>
            <thead>
              <tr>
                <TH>{t('evolutionConsole.gateTargetCase')}</TH>
                <TH>{t('evolutionConsole.gateParent')}</TH>
                <TH>{t('evolutionConsole.gateCandidate')}</TH>
              </tr>
            </thead>
            <tbody>
              {(gate.targets ?? []).map((tg: GateTarget) => (
                <tr key={tg.case}>
                  <td style={{ padding: '2px 8px' }}>
                    <code>{tg.case}</code>
                  </td>
                  <td style={{ padding: '2px 8px' }}>{tg.parent ?? '-'}</td>
                  <td style={{ padding: '2px 8px' }}>
                    {tg.candidate ?? '-'}{' '}
                    {!tg.ok && <Tag color="error">✗</Tag>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.gateJitter')}</h4>
      {(() => {
        const jp = jitter.parent ?? [];
        const jc = jitter.candidate ?? [];
        const jt = jitter.targets ?? [];
        const anyJitter = jp.length + jc.length + jt.length > 0;
        return (
          <Alert
            type={anyJitter ? 'warning' : 'success'}
            showIcon
            style={{ marginBottom: 8 }}
            message={
              <Space wrap>
                <span>{t('evolutionConsole.gateParent')}：{jp.join(', ') || t('evolutionConsole.gateNoText')}</span>
                <span>{t('evolutionConsole.gateCandidate')}：{jc.join(', ') || t('evolutionConsole.gateNoText')}</span>
                <span>{t('evolutionConsole.gateJitterTargets')}：{jt.join(', ') || t('evolutionConsole.gateNoText')}</span>
              </Space>
            }
            description={anyJitter ? t('evolutionConsole.gateJitterNote') : undefined}
          />
        );
      })()}

      {probeRows.length > 0 && (
        <>
          <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.gateProbe')}</h4>
          <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 4px' }}>
            {t('evolutionConsole.gateProbeNote')}
          </p>
          <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
            {probeRows.map(([name, v]) => {
              const unstable = (v?.identical ?? 0) < (v?.total ?? 0);
              return (
                <li key={name}>
                  <code>{name}</code>：{v?.identical}/{v?.total}
                  {unstable && <Tag color="warning" style={{ marginLeft: 8 }}>⚠️ {t('evolutionConsole.gateProbeUnstable')}</Tag>}
                </li>
              );
            })}
          </ul>
        </>
      )}

      {(gate.reasons?.length ?? 0) > 0 && (
        <>
          <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.gateReasons')}</h4>
          <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
            {(gate.reasons ?? []).map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
