import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Empty, Select, Table } from 'antd';
import type { TableColumnsType } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  getAllCallScopeObjects,
  getCallStats,
  getCallTargets,
  getCallTrend,
  getCapabilityStats,
  getOpenCapabilities,
  type PmCallScope,
} from '../../../api/platformMonitor';
import useAsyncData from '@/hooks/useAsyncData';
import useMetaFilters from '../useMetaFilters';
import { PM_ALL, PM_OBJECT_PAGE_SIZE } from '../../../constants/platformMonitor';
import { fmtCompact, fmtCount, fmtLatencyOrDash, fmtPercent, fmtPercent1, fmtPercentOrDash } from '../format';
import { capabilityLabel, duplicateProtocolsByLabel } from '../capabilityLabels';
import { buildScopeObjectOptions } from '../identifierOptions';
import { IdentifierWithProtocols } from './IdentifierText';
import SectionCard from './SectionCard';
import StatCards from './StatCards';
import SegmentedFilter from './SegmentedFilter';
import TrendAreaChart from './TrendAreaChart';
import ObjectDetailDrawer from './ObjectDetailDrawer';
import ModuleError from './ModuleError';
import MetaHint from './MetaHint';
import type { PmCallProtocol, PmCallScopeRow, PmKpiCard, PmPaneProps, PmTrendMetric } from '../types';

const TREND_METRICS: { value: PmTrendMetric; labelKey: string }[] = [
  { value: 'volume', labelKey: 'platformMonitor.call.metric.volume' },
  { value: 'successRate', labelKey: 'platformMonitor.call.metric.successRate' },
  { value: 'avgDuration', labelKey: 'platformMonitor.call.metric.avgDuration' },
  { value: 'failures', labelKey: 'platformMonitor.call.metric.failures' },
];

/**
 * 协议下拉的静态兜底候选（M1 元数据拿不到时使用）。
 *
 * `invoke` = 普通开放能力调用，`mcp` = MCP 工具调用（对象标识为工具名），
 * `openai` = 外部依赖。`http` 后端仍无写入方，不提供。
 */
export const PROTOCOLS: PmCallProtocol[] = ['invoke', 'mcp', 'openai'];

interface Props extends PmPaneProps {
  scope: PmCallScope;
  /** 失败数下钻到调用记录：切二级页签并带上筛选，由 CallMonitor 实现 */
  onDrillFailures?: () => void;
  /** 对象详情「查看全部调用」：切到记录页并按对象标识精确筛选，由 CallMonitor 实现 */
  onDrillObjectCalls?: (objectId: string, protocol: PmCallProtocol | null) => void;
}

/**
 * 调用监控里「外部依赖」与「开放能力」两个二级页签的**同一块内容**。
 *
 * 两者结构完全一致（4 KPI + 趋势 + 明细表），差异只有三处：取数函数、表的第二列、
 * 以及「全部对象 / 全部能力」的文案 —— 用一个 `scope` 开关收在一处，
 * 比拆成两个几乎相同的文件更能保证它们不会各自漂移。
 */
export default function CallScopeView({
  scope,
  window,
  refreshKey,
  manualRefreshKey,
  onDrillFailures,
  onDrillObjectCalls,
}: Props) {
  const { t } = useTranslation();
  const isTarget = scope === 'target';
  const meta = useMetaFilters();

  const [metric, setMetric] = useState<PmTrendMetric>('volume');
  /** 趋势曲线的对象筛选；PM_ALL 即全量 */
  const [trendTarget, setTrendTarget] = useState<string>(PM_ALL);
  /** 明细表的协议筛选，**下发到接口**（只有开放能力侧有这个控件） */
  const [protocol, setProtocol] = useState<string>(PM_ALL);
  const [page, setPage] = useState(1);
  const [detailRow, setDetailRow] = useState<PmCallScopeRow | null>(null);

  // 筛选或时间窗一变就回第一页：停在第 3 页而结果只剩 1 页会是一片空白
  useEffect(() => {
    setPage(1);
  }, [protocol, window]);

  // 统计卡与明细表**分开取**：统计是整窗口口径，与页码无关。曾经用一个 Promise.all
  // 一起取，于是翻页也会把 KPI 卡打回骨架态、并白跑一次统计请求。
  // 手动刷新进依赖（要看得见的 loading），定时刷新走 silentKey（原地换数，不闪骨架屏）
  const loadStats = useCallback(
    () => (isTarget ? getCallStats(window) : getCapabilityStats(window)),
    // manualRefreshKey 在函数体里没出现，但必须有：它变了才要重新取数
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isTarget, window, manualRefreshKey],
  );
  const {
    data: stats,
    loading: statsLoading,
    error: statsError,
    reload: reloadStats,
  } = useAsyncData(loadStats, { silentKey: refreshKey });

  const loadRows = useCallback(
    () =>
      isTarget
        ? getCallTargets(window, page, PM_OBJECT_PAGE_SIZE)
        : getOpenCapabilities(
            window,
            page,
            PM_OBJECT_PAGE_SIZE,
            protocol === PM_ALL ? undefined : (protocol as PmCallProtocol),
          ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isTarget, window, page, protocol, manualRefreshKey],
  );
  const {
    data: rowsData,
    loading: rowsLoading,
    error: rowsError,
    reload: reloadRows,
  } = useAsyncData(loadRows, { silentKey: refreshKey });

  const loadTrend = useCallback(
    // direction 必传：两个页签共用 C5，不传就是同一条全量曲线（见 api.ts getCallTrend）。
    // 外部依赖页再传 callScope=external：outbound 里还混着任务中心派活/回报等平台内部互调，
    // 与统计卡 / 对象列表的口径对齐。开放能力页（inbound）不传。
    () =>
      getCallTrend(
        window,
        metric,
        isTarget ? 'outbound' : 'inbound',
        trendTarget === PM_ALL ? undefined : trendTarget,
        isTarget ? 'external' : undefined,
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, metric, isTarget, trendTarget, manualRefreshKey],
  );
  const {
    data: trend,
    loading: trendLoading,
    error: trendError,
    reload: reloadTrend,
  } = useAsyncData(loadTrend, { silentKey: refreshKey });

  /** 趋势 tooltip 主数值格式化：成功率补 % 收两位，平均耗时补 ms 单位（见任务侧同款逻辑） */
  const trendValueFormat = useMemo(() => {
    if (metric === 'successRate') return fmtPercent;
    if (metric === 'avgDuration') return (value: number) => fmtLatencyOrDash(Math.round(value));
    return undefined;
  }, [metric]);

  /** Y 轴刻度：与 tooltip 同单位、但更短（成功率 1 位小数，延迟按 ms 取整不缩略） */
  const trendAxisFormat = useMemo(() => {
    if (metric === 'successRate') return fmtPercent1;
    if (metric === 'avgDuration') return (value: number) => `${Math.round(value)}ms`;
    return fmtCompact;
  }, [metric]);

  /**
   * 趋势卡下拉用的**全量**对象清单：独立按页拉对象列表，与明细表当前页解耦。
   *
   * 明细表一页只有 20 条，直接拿 `rows` 当选项时，翻页或对象超过一页后
   * 趋势卡就选不到窗口里的其它对象。这里翻完整清单，结果由 `useAsyncData`
   * 按窗口缓存；失败时页面回退到当前页的 `rows`，不把整块趋势卡打成错误态。
   */
  const loadAllObjects = useCallback(
    () => getAllCallScopeObjects(window, scope),
    // 只跟手动刷新（manualRefreshKey），不挂 silentKey：自动刷新每 60 秒递增 refreshKey，
    // 而这份清单最多翻 20 页，不该跟着自动刷新反复重拉。
    // manualRefreshKey 在函数体里没出现，但必须有：手动刷新时对象清单也要跟着重取
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, scope, manualRefreshKey],
  );
  const { data: allObjectsPage } = useAsyncData(loadAllObjects);
  /** null = 还没取到（取数中或失败），与「取到但为空」是两种状态 */
  const allObjects = allObjectsPage?.items ?? null;

  // 稳定引用：下面 targetOptions 以 rows 作依赖，每次渲染新数组会让 useMemo 形同虚设
  const rows = useMemo(() => rowsData?.items ?? [], [rowsData]);
  const total = rowsData?.total ?? 0;

  const kpiItems: PmKpiCard[] = useMemo(() => {
    if (!stats) return [];
    return [
      { key: 'total', label: t('platformMonitor.call.kpi.total'), value: fmtCount(stats.total) },
      {
        key: 'successRate',
        label: t('platformMonitor.call.kpi.successRate'),
        // 空窗口接口给 null，此时显示 0% 是错误表述（没有调用 ≠ 全部失败）
        value: fmtPercentOrDash(stats.successRate),
      },
      {
        key: 'avgLatency',
        label: t('platformMonitor.call.kpi.avgLatency'),
        value: fmtLatencyOrDash(stats.avgLatencyMs),
      },
      {
        key: 'failed',
        label: t('platformMonitor.call.kpi.failed'),
        value: fmtCount(stats.failed),
        // 契约里没有环比数据，这张卡就只有一个绝对值 —— 不给箭头，免得读成「失败数在涨」
        onClick: onDrillFailures,
      },
    ];
  }, [stats, t, onDrillFailures]);

  /**
   * 趋势卡下拉的候选对象名。
   *
   * 优先用全量清单；取到之前或失败时先用明细表当前页兜底 —— 下拉里哪怕只有当前页也比
   * 只剩「全部」可用。按名字去重：开放能力侧同一对象可能挂多个协议，清单里会出现两行。
   */
  const objectNames = useMemo(() => {
    const source = allObjects ?? rows;
    return [
      ...new Set(source.map((row) => row.name).filter((name): name is string => Boolean(name))),
    ];
  }, [allObjects, rows]);

  /** 同一展示名挂多个协议时，名称旁追加协议标签（仅这些名称才需要区分） */
  const protocolsByLabel = useMemo(
    () => duplicateProtocolsByLabel(allObjects ?? rows, t),
    [allObjects, rows, t],
  );

  /** 趋势卡的「全部对象 / 全部能力」下拉：value 保持原始标识，label 走能力释义 */
  const targetOptions = useMemo(() => {
    const allLabel = t(isTarget ? 'platformMonitor.call.allObjects' : 'platformMonitor.call.allCapabilities');
    if (objectNames.length) {
      return buildScopeObjectOptions(t, objectNames, allLabel);
    }
    // 全量清单**成功**返回空（空窗口 / 降级空清单）时给明确空态；
    // 仍在取数或取数失败时只留「全部」，不谎报「窗口里没有对象」。
    if (allObjects !== null) {
      return [
        { value: PM_ALL, label: allLabel },
        { value: '__empty__', label: t('platformMonitor.empty.noData'), disabled: true },
      ];
    }
    return [{ value: PM_ALL, label: allLabel }];
  }, [objectNames, allObjects, isTarget, t]);

  // 换窗口后原先选中的对象可能已不在清单里：接口会返回空曲线，下拉却挂着一个不存在的值
  useEffect(() => {
    if (trendTarget === PM_ALL || allObjects === null) return;
    if (!allObjects.some((row) => row.name === trendTarget)) setTrendTarget(PM_ALL);
  }, [allObjects, trendTarget]);

  /** 协议候选优先走 M1（后端补包后自动生效），拿不到就用枚举兜底 */
  const protocolOptions = useMemo(() => {
    const fromMeta = (meta?.objects ?? [])
      .map((item) => item.protocol)
      .filter((item): item is PmCallProtocol => item !== null && (PROTOCOLS as string[]).includes(item));
    const values = fromMeta.length ? [...new Set(fromMeta)] : PROTOCOLS;
    return [
      { value: PM_ALL, label: t('platformMonitor.records.allProtocols') },
      ...values.map((item) => ({ value: item, label: t(`platformMonitor.callProtocol.${item}`) })),
    ];
  }, [meta, t]);

  const columns = useMemo<TableColumnsType<PmCallScopeRow>>(() => {
    /**
     * 名称列显示释义；原始 ID 只在 hover 与复制入口。
     * 同名不同协议时追加协议标签 —— 否则同一展示名两行看起来完全一样。
     */
    const renderName = (value: string | null) => {
      const label = capabilityLabel(t, value);
      return (
        <IdentifierWithProtocols
          value={value}
          label={label}
          protocols={(value && protocolsByLabel.get(label)) || []}
        />
      );
    };

    const head: TableColumnsType<PmCallScopeRow> = isTarget
      ? [
          {
            title: t('platformMonitor.call.column.targetName'),
            dataIndex: 'name',
            render: renderName,
          },
        ]
      : [
          {
            title: t('platformMonitor.call.column.capabilityName'),
            dataIndex: 'name',
            render: renderName,
          },
          {
            // 接口传小写码，展示文案走 i18n
            title: t('platformMonitor.call.column.protocol'),
            dataIndex: 'protocol',
            render: (value: PmCallProtocol | null) =>
              value ? t(`platformMonitor.callProtocol.${value}`) : '-',
          },
        ];

    return [
      ...head,
      { title: t('platformMonitor.call.column.calls'), dataIndex: 'calls', align: 'right', render: fmtCount },
      {
        title: t('platformMonitor.call.column.successRate'),
        dataIndex: 'successRate',
        align: 'right',
        render: fmtPercentOrDash,
      },
      {
        title: t('platformMonitor.call.column.avgLatency'),
        dataIndex: 'avgLatencyMs',
        align: 'right',
        render: fmtLatencyOrDash,
      },
      { title: t('platformMonitor.call.column.failures'), dataIndex: 'failed', align: 'right', render: fmtCount },
      {
        title: t('platformMonitor.call.column.lastCallAt'),
        dataIndex: 'lastCallAt',
        render: (value: string | null) => value ?? '-',
      },
    ];
  }, [isTarget, t, protocolsByLabel]);

  return (
    <div className="yx-pm-pane">
      {/* 协议下拉依赖 M1；拿不到时静默兜底，这里补一句「候选可能不全」 */}
      <MetaHint />

      {/* 降级不是「没数据」：统计卡与曲线会照常出数、只是口径不完整，所以挂提示而不是走错误态 */}
      {stats?.moduleError ? (
        <Alert
          type="warning"
          showIcon
          message={t('platformMonitor.degraded.title')}
          description={stats.moduleError || t('platformMonitor.degraded.description')}
        />
      ) : null}

      {statsError ? (
        <ModuleError error={statsError} onRetry={reloadStats} />
      ) : (
        <StatCards items={kpiItems} loading={statsLoading} />
      )}

      <SectionCard
        title={t('platformMonitor.call.trend')}
        extra={
          <>
            <Select
              size="small"
              className="yx-pm-scope-select yx-pm-scope-select--wide"
              value={trendTarget}
              onChange={setTrendTarget}
              options={targetOptions}
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
          <TrendAreaChart
            data={trend?.points ?? []}
            loading={trendLoading}
            height={264}
            valueName={t(TREND_METRICS.find((item) => item.value === metric)?.labelKey ?? '')}
            valueFormat={trendValueFormat}
            yFormat={trendAxisFormat}
          />
        )}
      </SectionCard>

      <SectionCard
        title={t(isTarget ? 'platformMonitor.call.targets' : 'platformMonitor.call.capabilities')}
        hint={t('platformMonitor.call.rowCount', {
          n: fmtCount(total),
          unit: t(isTarget ? 'platformMonitor.call.unitObject' : 'platformMonitor.call.unitCapability'),
        })}
        extra={
          // 外部依赖侧没有筛选：`object_type` 是按对象精确匹配的，拿它做类型下拉是错的用法
          isTarget ? undefined : (
            <Select
              size="small"
              className="yx-pm-scope-select"
              value={protocol}
              onChange={setProtocol}
              options={protocolOptions}
            />
          )
        }
      >
        {rowsError ? (
          <ModuleError error={rowsError} onRetry={reloadRows} />
        ) : (
          <Table
            size="small"
            // 后端按 (target, protocol) 分组，同一对象标识理论上可挂两个协议；
            // 空名用空串兜底，空名行本身不可下钻
            rowKey={(row) => `${row.name ?? ''}:${row.protocol ?? ''}`}
            loading={rowsLoading}
            // 取数期间**保留**旧行，只靠 loading 的遮罩压住：清空会让表格从 N 行塌成空表，
            // 翻页与切筛选时整页高度跟着起伏（任务/Token 两张表也是这么做的）
            dataSource={rows}
            locale={{ emptyText: rowsLoading ? null : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('platformMonitor.empty.noData')} /> }}
            columns={columns}
            // 空名行没有可下钻的对象标识 ⇒ 不给手型光标、不挂点击
            rowClassName={(row) => (row.name ? 'yx-pm-row-clickable' : '')}
            onRow={(row) => (row.name ? { onClick: () => setDetailRow(row) } : {})}
            pagination={{
              current: page,
              pageSize: PM_OBJECT_PAGE_SIZE,
              total,
              onChange: setPage,
              showSizeChanger: false,
              size: 'small',
              // 对象数常年不超过一页，这时分页器只是一条噪音（设计稿里本来也没有）
              hideOnSinglePage: true,
            }}
          />
        )}
      </SectionCard>

      <ObjectDetailDrawer
        open={detailRow !== null}
        objectId={detailRow?.name ?? null}
        // 开放能力侧同一个名字可能挂在多个协议下，带上 protocol 才能定位到这一行
        protocol={isTarget ? null : (detailRow?.protocol ?? null)}
        window={window}
        onViewAllCalls={
          detailRow?.name && onDrillObjectCalls
            ? () => {
                onDrillObjectCalls(detailRow.name as string, isTarget ? null : detailRow.protocol);
                setDetailRow(null);
              }
            : undefined
        }
        onClose={() => setDetailRow(null)}
      />
    </div>
  );
}
