import { useCallback } from 'react';
import { Alert, Flex, Tag, Typography } from 'antd';
import type { DescriptionsProps } from 'antd';
import { formatDateTime } from '@jonex/shared-lib';
import { useTranslation } from 'react-i18next';
import { getNl2SqlDetail } from '@/api/dataOperations';
import SqlPre from '@/pages/EvolutionConsole/components/SqlPre';
import { A1bTag, NL2SQL_DASH, OutcomeTag, SourceText } from '../components/Nl2SqlCells';
import { fmtDuration } from '../utils';
import DetailDrawer from './DetailDrawer';
import { DASH } from './format';
import { DetailHeader, DetailSection } from './fields';
import useDetail from './useDetail';

const { Text } = Typography;

interface Props {
  /** 观测 id；null = 没有选中行 */
  id: string | null;
  open: boolean;
  onClose: () => void;
  spaceId: string | null | undefined;
  /** 行所属知识库，用于把详情钉在成员库内（后端 404 防探测口径） */
  kbId: string | null;
}

/** near_miss 条目是 `{entity_name, data_source_id, score}`；模型回坏 JSON 时也允许是任意值 */
function formatNearMiss(entry: unknown): string {
  if (!entry || typeof entry !== 'object') return String(entry);
  const row = entry as Record<string, unknown>;
  const name = typeof row.entity_name === 'string' ? row.entity_name : '';
  const source = typeof row.data_source_id === 'string' ? row.data_source_id : '';
  const score = typeof row.score === 'number' ? ` · ${row.score}` : '';
  return `${name || DASH}${source ? ` (${source})` : ''}${score}`;
}

/** trajectory 条目是 ToolRecorder 的 `{attempt, tool, args, flags, result}`；坏形状原样兜底 */
function formatTrajectory(entry: unknown): { tool: string; meta: string } {
  if (!entry || typeof entry !== 'object') return { tool: String(entry), meta: '' };
  const call = entry as Record<string, unknown>;
  const tool = typeof call.tool === 'string' ? call.tool : '';
  const attempt = typeof call.attempt === 'number' ? `#${call.attempt}` : '';
  const flags = Array.isArray(call.flags) ? call.flags.filter((flag) => flag !== 'ok').map(String) : [];
  const meta = [attempt, flags.join(', ')].filter(Boolean).join(' · ');
  return { tool: tool || DASH, meta };
}

/**
 * 抽屉 E · 问答现场（设计 §6.2）。
 *
 * 打开时按 id 取详情（与列表同空间/同库 scope）；内容只读 —— 观测页不提供
 * 「重试 / 重跑」入口。降级空壳里 `question` / `data_source_id` / `outcome`
 * 可能都是 null，逐格显示占位，不假设非空。
 */
export default function Nl2SqlDetailDrawer({ id, open, onClose, spaceId, kbId }: Props) {
  const { t } = useTranslation();

  const load = useCallback(
    (observationId: string) => getNl2SqlDetail(observationId, spaceId, kbId),
    [spaceId, kbId],
  );
  const { data, loading, error, retry } = useDetail(open && id ? id : null, load);

  const basicItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'time',
          label: t('dataOperations.nl2sql.column.time'),
          children: data.created_at ? formatDateTime(data.created_at) : DASH,
        },
        {
          key: 'kb',
          label: t('dataOperations.nl2sql.column.kb'),
          children: data.kb_name || data.knowledge_base_id || DASH,
        },
        {
          key: 'source',
          label: t('dataOperations.nl2sql.column.source'),
          children: <SourceText dataSourceId={data.data_source_id} sourceName={data.source_name} />,
        },
        {
          key: 'outcome',
          label: t('dataOperations.nl2sql.column.outcome'),
          children: <OutcomeTag outcome={data.outcome} />,
        },
        {
          key: 'rowCount',
          label: t('dataOperations.nl2sql.column.rowCount'),
          children: data.row_count == null ? DASH : data.row_count.toLocaleString(),
        },
        {
          key: 'a1b',
          label: t('dataOperations.nl2sql.column.a1b'),
          children: <A1bTag retried={data.precompile_retried} />,
        },
      ]
    : [];

  const timingItems: DescriptionsProps['items'] = data
    ? [
        {
          key: 'sqlDuration',
          label: t('dataOperations.nl2sql.column.sqlDuration'),
          children: <span className="yx-do-num">{fmtDuration(data.elapsed_ms)}</span>,
        },
        {
          key: 'totalDuration',
          label: t('dataOperations.nl2sql.column.totalDuration'),
          children: <span className="yx-do-num">{fmtDuration(data.total_ms)}</span>,
        },
      ]
    : [];

  const scene = (data?.scene && typeof data.scene === 'object' ? data.scene : {}) as Record<string, unknown>;
  const nearMiss = Array.isArray(scene.near_miss) ? scene.near_miss : [];
  const trajectory = Array.isArray(scene.trajectory) ? scene.trajectory : [];
  const trajectoryTruncated = scene.trajectory_truncated === true;
  // 降级空壳的三个关键字段同时为 null；只提示一次，不逐格重复
  const degraded = data != null && !data.question && !data.outcome && !data.data_source_id;

  return (
    <DetailDrawer
      open={open}
      title={t('dataOperations.nl2sql.drawer.title')}
      onClose={onClose}
      loading={loading}
      error={error}
      onRetry={retry}
    >
      {data && (
        <div className="yx-do-detail">
          <DetailHeader
            title={data.question || t('dataOperations.nl2sql.drawer.questionMissing')}
            meta={`${t('dataOperations.nl2sql.drawer.id')}: ${data.id || DASH}`}
          />

          {degraded && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message={t('dataOperations.nl2sql.drawer.degraded')}
            />
          )}

          <DetailSection items={basicItems} />
          <DetailSection title={t('dataOperations.nl2sql.drawer.timing')} items={timingItems} />

          <DetailSection title={t('dataOperations.nl2sql.drawer.question')}>
            <p className="yx-do-nl2sql-question">{data.question || NL2SQL_DASH}</p>
          </DetailSection>

          <DetailSection title={t('dataOperations.nl2sql.drawer.sql')}>
            {data.sql ? (
              <SqlPre text={data.sql} />
            ) : (
              <Text type="secondary">{t('dataOperations.nl2sql.drawer.emptySql')}</Text>
            )}

            {data.rejected_sql && (
              <div style={{ marginTop: 12 }}>
                <h4 className="yx-do-detail-subtitle">{t('dataOperations.nl2sql.drawer.rejectedSql')}</h4>
                <SqlPre text={data.rejected_sql} />
              </div>
            )}
          </DetailSection>

          {data.reason && (
            <DetailSection title={t('dataOperations.nl2sql.drawer.reason')}>
              <p className="yx-do-nl2sql-question">{data.reason}</p>
            </DetailSection>
          )}

          <DetailSection title={t('dataOperations.nl2sql.drawer.scene')}>
            <div className="yx-do-nl2sql-scene">
              <div>
                <div className="yx-do-nl2sql-scene-label">{t('dataOperations.nl2sql.column.a1b')}</div>
                <Flex wrap gap={8} align="center">
                  {data.precompile_violation ? (
                    <Tag color="error">{data.precompile_violation}</Tag>
                  ) : (
                    <Text type="secondary">{DASH}</Text>
                  )}
                  {data.precompile_retried && (
                    <Tag color="warning">{t('dataOperations.nl2sql.a1b.retried')}</Tag>
                  )}
                </Flex>
              </div>

              <div>
                <div className="yx-do-nl2sql-scene-label">{t('dataOperations.nl2sql.drawer.nearMiss')}</div>
                {nearMiss.length ? (
                  <Flex wrap gap={8}>
                    {nearMiss.map((entry, index) => (
                      <Tag key={`near-${index}`}>{formatNearMiss(entry)}</Tag>
                    ))}
                  </Flex>
                ) : (
                  <Text type="secondary">{t('dataOperations.nl2sql.drawer.noNearMiss')}</Text>
                )}
              </div>

              <div>
                <div className="yx-do-nl2sql-scene-label">
                  {t('dataOperations.nl2sql.drawer.trajectory')}
                  {trajectoryTruncated && (
                    <Text type="secondary"> · {t('dataOperations.nl2sql.drawer.truncated')}</Text>
                  )}
                </div>
                {trajectory.length ? (
                  <Flex vertical gap={6}>
                    {trajectory.map((entry, index) => {
                      const call = formatTrajectory(entry);
                      return (
                        <Flex key={`call-${index}`} gap={8} align="center">
                          <Tag>{call.tool}</Tag>
                          {call.meta && <Text type="secondary">{call.meta}</Text>}
                        </Flex>
                      );
                    })}
                  </Flex>
                ) : (
                  <Text type="secondary">{t('dataOperations.nl2sql.drawer.noTrajectory')}</Text>
                )}
              </div>
            </div>
          </DetailSection>
        </div>
      )}
    </DetailDrawer>
  );
}
