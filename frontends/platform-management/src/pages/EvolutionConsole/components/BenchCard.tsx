import React from 'react';
import { Alert, Card, Collapse, Descriptions, Empty, Space, Spin, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import type { BenchArm, BenchResult } from '../../../api/evolution';
import SqlPre from './SqlPre';

export const benchStatusColor = (status?: string | null): string => {
  if (status === 'passed') return 'success';
  if (status === 'failed') return 'error';
  if (status === 'warn') return 'warning';
  if (status === 'pending' || status === 'parent_ran') return 'processing';
  return 'default';
};

export const benchOutcomeColor = (outcome?: string | null): string => {
  if (outcome === 'pass') return 'success';
  if (outcome === 'fail') return 'error';
  if (outcome === 'error') return 'warning';
  return 'default';
};

interface BenchCardProps {
  /** `run.bench`（采纳后完整语料基准）；未跑/受理中为 null。 */
  bench: BenchResult | null;
  /** `run.bench_status`：pending / parent_ran / passed / failed / warn / skipped。 */
  benchStatus?: string | null;
  /** `run.bench_mode`：enforce（失败阻断） / warn（失败只告警）。 */
  benchMode?: string | null;
}

export interface CaseRow {
  name: string;
  parent: string | null;
  candidate: string | null;
  reason_code: string | null;
  sql: string | null;
}

const armFraction = (arm?: BenchArm | null) =>
  arm ? `${arm.passes}/${arm.graded}` : '-';

/** 两臂逐题对齐：父本顺序优先，候选独有的题追加在后。
 *
 * 区分三种「没有结论」：
 * - 该臂整体没跑（arm null）→ `null`（渲染 `-`）；
 * - 候选臂在、候选缺这题 → `error`（与后端 `compare_arms` 同口径：父本通过
 *   而候选缺题按 error / new_error 计，表格不能显示成「没跑过」）；
 * - 父本臂在、父本缺这题（候选独有）→ `null`：后端 `compare_arms` 只按
 *   父本行对齐，不把候选独有题算成父本 error。
 *
 * 原因码只取候选臂：候选缺失时不能回落到父本的原因码，否则会把父本的
 * 失败原因错误地挂到候选缺失那一侧。SQL 同理：候选臂在但缺这题时显示父本
 * SQL，会让人误以为候选也执行过这条 SQL；只有父本独有题（候选整臂未跑）
 * 才显示父本 SQL。 */
export const buildCaseRows = (bench: BenchResult): CaseRow[] => {
  const parentArm = bench.parent ?? null;
  const candidateArm = bench.candidate ?? null;
  const parent = new Map((parentArm?.cases ?? []).map((c) => [c.name, c]));
  const candidate = new Map((candidateArm?.cases ?? []).map((c) => [c.name, c]));
  const names = [
    ...parent.keys(),
    ...[...candidate.keys()].filter((name) => !parent.has(name)),
  ];
  return names.map((name) => {
    const parentCase = parent.get(name);
    const cand = candidate.get(name);
    let parentOutcome: string | null = null;
    if (parentArm && parentCase) parentOutcome = parentCase.outcome || 'error';
    let candidateOutcome: string | null = null;
    if (candidateArm) {
      // 候选臂在但缺题 → error；有题则按结果（空结果同样按 error）。
      candidateOutcome = cand ? (cand.outcome || 'error') : 'error';
    }
    return {
      name,
      parent: parentOutcome,
      candidate: candidateOutcome,
      reason_code: cand?.reason_code ?? null,
      sql: cand?.sql ?? (candidateArm ? null : parentCase?.sql ?? null),
    };
  });
};

/** 采纳后完整语料基准卡：五态（未跑 / 进行中 / 通过 / 失败·告警 / 跳过）+ 逐题折叠表。 */
export default function BenchCard({ bench, benchStatus, benchMode }: BenchCardProps) {
  const { t } = useTranslation();
  // 老 run 可能只有 `bench`、没有 `bench_status` 列：按结果反推，别误报「未跑」。
  const inferredStatus = bench
    ? ((bench.new_red?.length ?? 0) > 0
      || (bench.new_error?.length ?? 0) > 0
      || (bench.reasons?.length ?? 0) > 0 ? 'failed' : 'passed')
    : null;
  const status = benchStatus ?? inferredStatus;
  const mode = benchMode ?? bench?.bench_mode ?? null;

  // 状态条用短标签（`benchStatus.*`）；正文各态用 `benchPassed/benchFailed/...`
  // 的完整文案，避免同一句话在标题与正文里重复出现。
  const statusLabel = (value: string | null) => (value
    ? t(`evolutionConsole.benchStatus.${value}`, { defaultValue: value })
    : null);

  const headerTag = status ? (
    <Tag color={benchStatusColor(status)}>
      {statusLabel(status) ?? status}
    </Tag>
  ) : (
    <Tag>{t('evolutionConsole.benchNotRun')}</Tag>
  );

  const header = (
    <Space size={8}>
      <span>{t('evolutionConsole.benchTitle')}</span>
      {headerTag}
    </Space>
  );

  if (!bench && !status) {
    return (
      <Card size="small" style={{ marginBottom: 16 }} title={header}>
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
               description={t('evolutionConsole.benchNotRunHint')} />
      </Card>
    );
  }

  const inProgress = status === 'pending' || status === 'parent_ran';
  const parentArm = bench?.parent ?? null;
  const candidateArm = bench?.candidate ?? null;
  const newRed = bench?.new_red ?? [];
  const newError = bench?.new_error ?? [];
  const reasons = bench?.reasons ?? [];
  const flaky = bench?.flaky ?? [];
  const caseRows = bench ? buildCaseRows(bench) : [];
  const hasNoNewRed = newRed.length === 0 && newError.length === 0;

  const reasonList = reasons.length > 0 && (
    <>
      <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.benchReasons')}</h4>
      <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
        {reasons.map((reason) => (
          <li key={reason}>
            {t(`evolutionConsole.benchReason.${reason}`, { defaultValue: reason })}
          </li>
        ))}
      </ul>
    </>
  );

  const rulerCrossed = bench?.ruler_crossed ?? [];
  const rulerCrossedLine = rulerCrossed.length > 0 && (
    <div style={{ margin: '8px 0 4px', fontSize: 12 }}>
      <span style={{ color: '#8c8c8c', marginRight: 8 }}>
        {t('evolutionConsole.benchRulerCrossed')}
      </span>
      <Space size={4} wrap>
        {rulerCrossed.map((facet) => (
          <Tag key={facet} color="warning">
            {t(`evolutionConsole.benchRulerFacet.${facet}`, { defaultValue: facet })}
          </Tag>
        ))}
      </Space>
    </div>
  );

  const flakyList = flaky.length > 0 && (
    <>
      <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.benchFlaky')}</h4>
      <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
        {flaky.map((item) => (
          <li key={item.name}>
            <code>{item.name}</code>
            <span style={{ color: '#8c8c8c', fontSize: 12, marginLeft: 8 }}>
              {item.first ?? '-'} → {item.retry ?? '-'}
            </span>
          </li>
        ))}
      </ul>
    </>
  );

  const caseColumns: ColumnsType<CaseRow> = [
    {
      title: t('evolutionConsole.benchCase'), dataIndex: 'name', width: 180,
      render: (value: string) => <code>{value}</code>,
    },
    {
      title: t('evolutionConsole.benchParent'), dataIndex: 'parent', width: 90,
      render: (value: string | null) => (
        <Tag color={benchOutcomeColor(value)}>
          {value ? t(`evolutionConsole.benchOutcome.${value}`, { defaultValue: value }) : '-'}
        </Tag>
      ),
    },
    {
      title: t('evolutionConsole.benchCandidate'), dataIndex: 'candidate', width: 90,
      render: (value: string | null) => (
        <Tag color={benchOutcomeColor(value)}>
          {value ? t(`evolutionConsole.benchOutcome.${value}`, { defaultValue: value }) : '-'}
        </Tag>
      ),
    },
    {
      title: t('evolutionConsole.benchReasonCode'), dataIndex: 'reason_code', width: 150,
      render: (value: string | null) => value || '-',
    },
    {
      title: t('evolutionConsole.benchSql'), dataIndex: 'sql',
      render: (value: string | null) => (
        value ? (
          <details>
            <summary style={{ cursor: 'pointer', fontSize: 12 }}>SQL</summary>
            <SqlPre text={value} maxHeight={160} />
          </details>
        ) : '-'
      ),
    },
  ];

  const casesBlock = caseRows.length > 0 && (
    <Collapse
      style={{ marginTop: 8 }}
      items={[{
        key: 'cases',
        label: `${t('evolutionConsole.benchCases')}（${caseRows.length}）`,
        children: (
          <Table<CaseRow>
            rowKey="name"
            size="small"
            pagination={false}
            columns={caseColumns}
            dataSource={caseRows}
          />
        ),
      }]}
    />
  );

  const meta = (
    <Descriptions size="small" column={3} style={{ marginBottom: 8 }}>
      <Descriptions.Item label={t('evolutionConsole.benchMode')}>
        {mode
          ? t(`evolutionConsole.benchModeLabel.${mode}`, { defaultValue: mode })
          : '-'}
      </Descriptions.Item>
      <Descriptions.Item label={t('evolutionConsole.benchCorpus')}>
        {typeof bench?.corpus_size === 'number' ? bench.corpus_size : '-'}
      </Descriptions.Item>
      <Descriptions.Item label={t('evolutionConsole.benchDuration')}>
        {typeof bench?.duration_ms === 'number' ? `${bench.duration_ms} ms` : '-'}
      </Descriptions.Item>
    </Descriptions>
  );

  let body: React.ReactNode;
  if (inProgress) {
    body = (
      <Space>
        <Spin size="small" />
        <span>{t('evolutionConsole.benchRunning')}</span>
        <span style={{ color: '#8c8c8c', fontSize: 12 }}>
          {t('evolutionConsole.benchParent')} {armFraction(parentArm)}
          {' · '}
          {t('evolutionConsole.benchCandidate')} {armFraction(candidateArm)}
        </span>
      </Space>
    );
  } else if (status === 'skipped') {
    body = (
      <Alert
        type="warning"
        showIcon
        message={t('evolutionConsole.benchSkipped')}
        description={bench?.override
          ? <Tag color="warning">{t('evolutionConsole.benchSkippedOverride')}</Tag>
          : undefined}
      />
    );
  } else if (status === 'passed') {
    const summary = `${t('evolutionConsole.benchParent')} ${armFraction(parentArm)} → `
      + `${t('evolutionConsole.benchCandidate')} ${armFraction(candidateArm)} · `
      + `${hasNoNewRed ? t('evolutionConsole.benchNoNewRed') : t('evolutionConsole.benchNewRed')} · `
      + t('evolutionConsole.benchPassed');
    body = <Alert type="success" showIcon message={<span>{summary}</span>} />;
  } else {
    const tone = status === 'warn' ? 'warning' : 'error';
    const headline = `${t('evolutionConsole.benchParent')} ${armFraction(parentArm)} → `
      + `${t('evolutionConsole.benchCandidate')} ${armFraction(candidateArm)}`;
    body = (
      <>
        <Alert
          type={tone}
          showIcon
          message={<span>{`${t(status === 'warn'
            ? 'evolutionConsole.benchWarn'
            : 'evolutionConsole.benchFailed')} · ${headline}`}</span>}
        />
        {newRed.length > 0 && (
          <>
            <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.benchNewRed')}</h4>
            <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
              {newRed.map((name) => <li key={name}><code>{name}</code></li>)}
            </ul>
          </>
        )}
        {newError.length > 0 && (
          <>
            <h4 style={{ margin: '8px 0 4px' }}>{t('evolutionConsole.benchNewError')}</h4>
            <ul style={{ margin: '4px 0', paddingLeft: 20 }}>
              {newError.map((name) => <li key={name}><code>{name}</code></li>)}
            </ul>
          </>
        )}
        {reasonList}
        {rulerCrossedLine}
      </>
    );
  }

  return (
    <Card size="small" style={{ marginBottom: 16 }} title={header}>
      {meta}
      {body}
      {flakyList}
      {casesBlock}
    </Card>
  );
}
