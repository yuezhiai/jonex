import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Empty, List, Select, Table } from 'antd';
import type { TableColumnsType } from 'antd';
import { RightOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import {
  getTaskFailures,
  getTaskOverview,
  getTaskStats,
  getTaskTrend,
  pmWindowBounds,
} from '../../api/platformMonitor';
import useAsyncData from '@/hooks/useAsyncData';
import useMetaFilters from './useMetaFilters';
import { PM_ALL } from '../../constants/platformMonitor';
import { fmtCount, fmtDurationOrDash, fmtPercent1OrDash } from './format';
import SectionCard from './components/SectionCard';
import StatCards from './components/StatCards';
import SegmentedFilter from './components/SegmentedFilter';
import TrendAreaChart from './components/TrendAreaChart';
import ModuleError from './components/ModuleError';
import MetaHint from './components/MetaHint';
import { FailureReasonTag } from './components/Tags';
import type { PmKpiCard, PmPaneProps, PmTrendMetric } from './types';

/** 概览树节点：父行是任务类型，子行是它的各阶段；两者字段同构，阶段行没有 `total` */
interface OverviewNode {
  key: string;
  name: string;
  total?: number;
  running: number;
  waiting: number;
  failed: number;
  successRate: number | null;
  avgDurationSec: number | null;
  children?: OverviewNode[];
}

const TREND_METRICS: { value: PmTrendMetric; labelKey: string }[] = [
  { value: 'volume', labelKey: 'platformMonitor.task.metric.volume' },
  { value: 'successRate', labelKey: 'platformMonitor.task.metric.successRate' },
  { value: 'avgDuration', labelKey: 'platformMonitor.task.metric.avgDuration' },
  { value: 'failures', labelKey: 'platformMonitor.task.metric.failures' },
];

export default function TaskMonitor({ window, refreshKey, onLoaded }: PmPaneProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const meta = useMetaFilters();

  const [metric, setMetric] = useState<PmTrendMetric>('volume');
  /** 趋势的任务类型筛选；PM_ALL 即全部任务（不传 `task_type`） */
  const [trendTaskType, setTrendTaskType] = useState<string>(PM_ALL);
  /** 失败列表的任务类型筛选。与趋势各管各的：两张卡挨着放，共用一个值会让人以为改一个动两处 */
  const [failureTaskType, setFailureTaskType] = useState<string>(PM_ALL);

  const loadStats = useCallback(
    () => getTaskStats(window),
    // refreshKey 在函数体里没出现，但必须有：它变了才要重新取数
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, refreshKey],
  );
  const { data: stats, loading: statsLoading, error: statsError, reload: reloadStats } = useAsyncData(loadStats);

  const loadTrend = useCallback(
    () => getTaskTrend(window, metric, trendTaskType === PM_ALL ? undefined : trendTaskType),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, metric, trendTaskType, refreshKey],
  );
  const { data: trend, loading: trendLoading, error: trendError, reload: reloadTrend } = useAsyncData(loadTrend);

  const loadFailures = useCallback(
    () => getTaskFailures(window, failureTaskType === PM_ALL ? undefined : failureTaskType),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, failureTaskType, refreshKey],
  );
  const {
    data: failures,
    loading: failuresLoading,
    error: failuresError,
    reload: reloadFailures,
  } = useAsyncData(loadFailures);

  const loadOverview = useCallback(
    () => getTaskOverview(window),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, refreshKey],
  );
  const {
    data: overview,
    loading: overviewLoading,
    error: overviewError,
    reload: reloadOverview,
  } = useAsyncData(loadOverview);

  // 工具条的「数据更新于」跟统计卡走：as-of 是后端落库时刻，不是列表的取数次。
  // 失败时传 undefined（保留上一份），不要因为一次失败把时间戳清空。
  useEffect(() => {
    if (statsLoading) return;
    onLoaded(stats?.asOf);
  }, [statsLoading, stats, onLoaded]);

  const kpiItems: PmKpiCard[] = useMemo(() => {
    if (!stats) return [];
    return [
      {
        key: 'total',
        label: t('platformMonitor.task.kpi.total'),
        value: fmtCount(stats.total),
      },
      {
        key: 'successRate',
        label: t('platformMonitor.task.kpi.successRate'),
        // 空窗口后端给 null，此时 0% 是错误表述（没有任务结束 ≠ 全部失败）
        value: fmtPercent1OrDash(stats.successRate),
      },
      {
        key: 'avgDuration',
        label: t('platformMonitor.task.kpi.avgDuration'),
        value: fmtDurationOrDash(stats.avgDurationSec, t),
      },
      {
        key: 'waiting',
        label: t('platformMonitor.task.kpi.waiting'),
        // 只报一个实时数：本卡是快照（不套时间窗），等待占用本身不做细分
        value: fmtCount(stats.waiting),
        badge: t('platformMonitor.realtime'),
      },
    ];
  }, [stats, t]);

  /**
   * 失败任务徽标取**接口的 total**，不再对概览表「失败」列求和。
   *
   * 一旦按任务类型筛过，列表长度只是筛选结果，而概览求和是全量 —— 两个数放在一起
   * 会变成「标题 438，列表 5 条」的错位。接口给了 total 就以它为准。
   */
  const failureTotal = failures?.total ?? 0;

  /** 任务目录 code → 展示名；M1 未返回时退回 code 本身，绝不拼成 i18n key */
  const taskTypeName = useCallback(
    (code: string) => meta?.taskTypes.find((item) => item.code === code)?.name ?? code,
    [meta],
  );

  /**
   * 深链到任务中心：带上当前窗口与「失败」状态，落地就是同一批失败任务。
   *
   * 不传任务类型 —— 任务中心的列表接口没有 `task_type` 入参，传了会被静默忽略，
   * 用户会以为筛过了（跳过去看到的是全部类型）。
   */
  const goTaskCenter = () => {
    const { from, to } = pmWindowBounds(window);
    const params = new URLSearchParams({ status: 'failed', from, to });
    navigate(`/task-center?${params.toString()}`);
  };

  const taskTypeOptions = useMemo(
    () => [
      { value: PM_ALL, label: t('platformMonitor.task.allTasks') },
      ...(meta?.taskTypes ?? []).map((item) => ({ value: item.code, label: item.name })),
    ],
    [meta, t],
  );

  /**
   * 概览表用**树形表格**，而不是「父表 + 展开行里再套一张表」：
   * 嵌套表的列宽由它自己算，和父表列宽毫无关系，展开项的数值必然错到表头外面去；
   * 树形子行与父行共用同一套列定义和表头，对齐由 antd 保证。
   */
  const overviewTree = useMemo<OverviewNode[]>(
    () =>
      (overview ?? []).map((row) => ({
        key: row.taskType,
        name: taskTypeName(row.taskType),
        total: row.total,
        running: row.running,
        waiting: row.waiting,
        failed: row.failed,
        successRate: row.successRate,
        avgDurationSec: row.avgDurationSec,
        children: row.stages.map((stage) => ({
          key: `${row.taskType}::${stage.stage}`,
          // 展示名走后端 `stage_name`；为空时才退回本地文案
          name: stage.stageName || t(`platformMonitor.stage.${stage.stage}`),
          running: stage.running,
          waiting: stage.waiting,
          failed: stage.failed,
          successRate: stage.successRate,
          avgDurationSec: stage.avgDurationSec,
        })),
      })),
    [overview, taskTypeName, t],
  );

  const overviewColumns: TableColumnsType<OverviewNode> = [
    { title: t('platformMonitor.task.column.taskType'), dataIndex: 'name' },
    {
      title: t('platformMonitor.column.total'),
      dataIndex: 'total',
      align: 'right',
      // 阶段行没有任务总数（阶段是任务内部的进度维度），留空而不是显示 0
      render: (value: number | undefined) => (value == null ? '-' : fmtCount(value)),
    },
    { title: t('platformMonitor.column.running'), dataIndex: 'running', align: 'right', render: fmtCount },
    { title: t('platformMonitor.column.waiting'), dataIndex: 'waiting', align: 'right', render: fmtCount },
    { title: t('platformMonitor.column.failed'), dataIndex: 'failed', align: 'right', render: fmtCount },
    {
      title: t('platformMonitor.column.successRate'),
      dataIndex: 'successRate',
      align: 'right',
      render: fmtPercent1OrDash,
    },
    {
      title: t('platformMonitor.column.avgDuration'),
      dataIndex: 'avgDurationSec',
      align: 'right',
      render: (value: number | null) => fmtDurationOrDash(value, t),
    },
  ];

  return (
    <div className="yx-pm-pane">
      {/* 任务类型下拉依赖 M1；拿不到时静默兜底，这里补一句「候选可能不全」 */}
      <MetaHint />

      {/* 降级不是「没数据」：统计卡会照常出空壳，挂提示而不是走错误态 */}
      {stats?.moduleError ? (
        <Alert
          type="warning"
          showIcon
          message={t('platformMonitor.degraded.title')}
          description={stats.moduleError || t('platformMonitor.degraded.description')}
        />
      ) : null}

      {/* 四块内容各自取数、各自担错：统计挂了不该让已经拿到的列表一起消失 */}
      {statsError ? <ModuleError error={statsError} onRetry={reloadStats} /> : <StatCards items={kpiItems} loading={statsLoading} />}

      <div className="yx-pm-two-col">
        <SectionCard
          title={t('platformMonitor.task.trend')}
          extra={
            <>
              <Select
                size="small"
                className="yx-pm-scope-select"
                value={trendTaskType}
                onChange={setTrendTaskType}
                // 候选来自 M1 的任务目录；拿不到时只剩「全部任务」，趋势仍可用
                options={taskTypeOptions}
              />
              <SegmentedFilter
                value={metric}
                onChange={setMetric}
                options={TREND_METRICS.map((item) => ({ value: item.value, label: t(item.labelKey) }))}
              />
            </>
          }
        >
          {trendError ? (
            <ModuleError error={trendError} onRetry={reloadTrend} />
          ) : (
            /* fill：图跟着卡片撑满。同排的失败任务卡比它高，卡高由对方决定，
               给固定 height 会让两卡底部差出一截（见规格 §10） */
            <TrendAreaChart
              data={trend ?? []}
              loading={trendLoading}
              fill
              valueName={t(TREND_METRICS.find((item) => item.value === metric)?.labelKey ?? '')}
            />
          )}
        </SectionCard>

        {/* 不做失败原因筛选：T3 没有 reason 入参，只筛已取回的 5 条会让「筛选结果」
            与标题上的总数对不上；原因仍以 tag 形式逐行展示 */}
        <SectionCard
          title={
            <span className="yx-pm-title-with-badge">
              {t('platformMonitor.task.failures')}
              <span className="yx-pm-count-badge">{fmtCount(failureTotal)}</span>
            </span>
          }
          extra={
            <Select
              size="small"
              className="yx-pm-scope-select"
              value={failureTaskType}
              onChange={setFailureTaskType}
              options={taskTypeOptions}
            />
          }
        >
          {failuresError ? (
            <ModuleError error={failuresError} onRetry={reloadFailures} />
          ) : (
            <List
              size="small"
              loading={failuresLoading}
              dataSource={failures?.items ?? []}
              locale={{
                emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('platformMonitor.empty.noData')} />,
              }}
              renderItem={(item) => (
                <List.Item className="yx-pm-failure-item">
                  <div className="yx-pm-failure-main">
                    {/* 「全部任务」视图下同一列表里混着不同目录，类型名要跟着每行走 */}
                    <span className="yx-pm-failure-id">
                      {taskTypeName(item.taskType)} · {item.taskId}
                    </span>
                    <span className="yx-pm-failure-name">{item.name}</span>
                  </div>
                  <FailureReasonTag reason={item.reason} />
                  <span className="yx-pm-failure-time">{item.time}</span>
                </List.Item>
              )}
            />
          )}
          {/* 任务中心是本应用的另一个路由，同应用内跳转交给 react-router（hosted 下 basename 由它处理） */}
          <div className="yx-pm-more" onClick={goTaskCenter} role="button" tabIndex={0}>
            {t('platformMonitor.task.moreToTaskCenter')}
            <RightOutlined />
          </div>
        </SectionCard>
      </div>

      <SectionCard title={t('platformMonitor.task.overview')} hint={t('platformMonitor.task.overviewHint')}>
        {overviewError ? (
          <ModuleError error={overviewError} onRetry={reloadOverview} />
        ) : (
          <Table
            size="small"
            rowKey="key"
            loading={overviewLoading}
            pagination={false}
            dataSource={overviewTree}
            columns={overviewColumns}
          />
        )}
      </SectionCard>
    </div>
  );
}
