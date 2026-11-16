import { Fragment } from 'react';
import { ArrowRightOutlined } from '@ant-design/icons';
import { Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DoPipelineOverview, DoPipelineStats, DoTaskStatus } from '../types';

interface Props {
  data: DoPipelineOverview | null;
  /** 状态分布与平均耗时 —— 后端按全量任务算好，不从任务明细现数（明细是 TOP-20） */
  stats: DoPipelineStats | null;
  loading?: boolean;
}

const STAGES: { key: keyof DoPipelineOverview; labelKey: string; accent: string }[] = [
  { key: 'total', labelKey: 'dataOperations.pipeline.total', accent: 'var(--yx-text-secondary)' },
  { key: 'parsed', labelKey: 'dataOperations.pipeline.parsed', accent: 'var(--yx-accent)' },
  { key: 'extracted', labelKey: 'dataOperations.pipeline.extracted', accent: '#8b5cf6' },
  { key: 'compiled', labelKey: 'dataOperations.pipeline.compiled', accent: '#10b981' },
];

const STATUS_ORDER: DoTaskStatus[] = ['success', 'failed', 'running', 'pending'];

/** 平均值由后端算好：只计成功任务，未完成的任务没有时长 */
function formatDuration(ms: number | null): string {
  if (!ms) return '—';
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** 分母为 0 时返回占位符，避免出现 NaN% */
function ratio(part: number, whole: number): string {
  return whole ? `${((part / whole) * 100).toFixed(1)}%` : '—';
}

/**
 * 数据处理流水的汇总：四级漏斗 + 阶段转化率 + 任务状态。
 * 明细列表不在这里渲染（产品要求流水只保留汇总口径）。
 */
export default function PipelineOverview({ data, stats, loading }: Props) {
  const { t } = useTranslation();

  if (loading) return <Skeleton active paragraph={{ rows: 3 }} />;
  if (!data || !stats) return null;

  const conversions = [
    { key: 'convExtract', part: data.extracted, whole: data.parsed },
    { key: 'convCompile', part: data.compiled, whole: data.extracted },
    { key: 'convTotal', part: data.compiled, whole: data.total },
  ];

  return (
    <>
      <div className="yx-do-pipeline">
        {STAGES.map((stage, index) => (
          <Fragment key={stage.key}>
            {index > 0 && <ArrowRightOutlined className="yx-do-pipeline-arrow" />}
            <div className="yx-do-pipeline-node" style={{ borderTopColor: stage.accent }}>
              <div className="yx-do-pipeline-value">{data[stage.key].toLocaleString()}</div>
              <div className="yx-do-pipeline-label">{t(stage.labelKey)}</div>
            </div>
          </Fragment>
        ))}
      </div>

      <div className="yx-do-metrics">
        <div className="yx-do-metric-row">
          <span className="yx-do-metric-label">{t('dataOperations.pipeline.conversionTitle')}</span>
          <span className="yx-do-metric-items">
            {conversions.map((item) => (
              <span key={item.key} className="yx-do-metric-item">
                <i>{t(`dataOperations.pipeline.${item.key}`)}</i>
                <em>{ratio(item.part, item.whole)}</em>
              </span>
            ))}
          </span>
        </div>

        <div className="yx-do-metric-row">
          <span className="yx-do-metric-label">{t('dataOperations.pipeline.taskStatus')}</span>
          <span className="yx-do-metric-items">
            {STATUS_ORDER.map((status) => (
              <span key={status} className="yx-do-metric-item">
                <i>{t(`status.${status}`)}</i>
                <em>{stats[status]}</em>
              </span>
            ))}
            <span className="yx-do-metric-item">
              <i>{t('dataOperations.pipeline.avgDuration')}</i>
              <em>{formatDuration(stats.avgDurationMs)}</em>
            </span>
          </span>
        </div>
      </div>
    </>
  );
}
