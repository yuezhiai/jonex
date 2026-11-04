import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Input, Table } from 'antd';
import type { TableColumnsType } from 'antd';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import {
  getModelShare,
  getModelUsage,
  getTokenStats,
  getTokenTrend,
  PM_MODEL_USAGE_SORT_FIELDS,
  type PmModelUsageSort,
  type PmModelUsageSortField,
  type PmTokenTrendKind,
} from '../../api/platformMonitor';
import useAsyncData from '@/hooks/useAsyncData';
import { PM_PAGE_SIZE } from '../../constants/platformMonitor';
import { resolveUsageSortChange } from './modelUsageSort';
import { fmtCompact, fmtCount, fmtPercent1OrDash } from './format';
import SectionCard from './components/SectionCard';
import FilterBar from './components/FilterBar';
import StatCards from './components/StatCards';
import SegmentedFilter from './components/SegmentedFilter';
import TrendAreaChart from './components/TrendAreaChart';
import UsageBarList from './components/UsageBarList';
import ModuleError from './components/ModuleError';
import type { PmKpiCard, PmModelUsageRow, PmPaneProps } from './types';

const TREND_KINDS: { value: PmTokenTrendKind; labelKey: string }[] = [
  { value: 'total', labelKey: 'platformMonitor.token.kind.total' },
  { value: 'input', labelKey: 'platformMonitor.token.kind.input' },
  { value: 'output', labelKey: 'platformMonitor.token.kind.output' },
];

export default function TokenMonitor({ window, refreshKey, manualRefreshKey }: PmPaneProps) {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();

  /**
   * 搜索词与排序的初值取自 URL（`?kw=&sort_by=&sort_order=`）：刷新与分享链接能还原同一视图。
   *
   * 只在挂载时读一次，之后由下面两个 effect 单向写回。页码**不进 URL** —— 换筛选会回到
   * 第一页，存一个可能已不存在的页码反而会打开一片空白。`sort_by` 是用户可改的，
   * 必须按白名单校验后再下发。
   */
  const [initial] = useState(() => {
    const field = searchParams.get('sort_by');
    const order = searchParams.get('sort_order');
    return {
      keyword: searchParams.get('kw') ?? '',
      sort: {
        field: (PM_MODEL_USAGE_SORT_FIELDS as readonly string[]).includes(field ?? '')
          ? (field as PmModelUsageSortField)
          : 'total_tokens',
        order: order === 'asc' ? 'asc' : 'desc',
      } satisfies PmModelUsageSort,
    };
  });

  const [kind, setKind] = useState<PmTokenTrendKind>('total');
  const [keyword, setKeyword] = useState(initial.keyword);
  const [query, setQuery] = useState(initial.keyword.trim());
  const [page, setPage] = useState(1);
  // 排序交给后端（K4 的 sort_by/sort_order）：只排当前页会得到跨页错位的假序
  const [sort, setSort] = useState<PmModelUsageSort>(initial.sort);
  // 点表头时先记住列（capture 阶段早于 antd 内部的 sorter 点击），
  // 因为 antd 在「取消排序」那一下的 onChange 里不带 columnKey
  const lastSortClickRef = useRef<PmModelUsageSortField | null>(null);
  /**
   * 第三态「取消排序」：表头箭头全灭、请求不带 sort_by/sort_order，
   * 后端按缺省（总 Token 降序）返回 —— 数据仍有顺序，只是没有「用户指定的排序」。
   * 刷新后回到带箭头的默认列（该态不写进 URL，避免链接里出现无意义的 sort=none）。
   */
  const [sortCleared, setSortCleared] = useState(false);

  // 输入框与真正下发的查询分开：每敲一个字就查一次会把后端打满，300ms 后再发
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(keyword.trim());
      // 换了搜索词就回第一页：停在第 3 页而结果只剩 1 页会是一片空白
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [keyword]);

  // 搜索词与排序写回 URL（单向：页面 → URL）。默认排序不写 —— 链接里挂一堆默认值只会更难读
  useEffect(() => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (query) next.set('kw', query);
        else next.delete('kw');
        if (sort.field === 'total_tokens' && sort.order === 'desc') {
          next.delete('sort_by');
          next.delete('sort_order');
        } else {
          next.set('sort_by', sort.field);
          next.set('sort_order', sort.order);
        }
        return next;
      },
      { replace: true },
    );
  }, [query, sort, setSearchParams]);

  // 手动刷新进依赖（要看得见的 loading），定时刷新走 silentKey（原地换数，不闪骨架屏）
  const loadStats = useCallback(
    () => getTokenStats(window),
    // manualRefreshKey 在函数体里没出现，但必须有：它变了才要重新取数
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, manualRefreshKey],
  );
  const {
    data: stats,
    loading: statsLoading,
    error: statsError,
    reload: reloadStats,
  } = useAsyncData(loadStats, { silentKey: refreshKey });

  const loadUsage = useCallback(
    () => getModelUsage(window, query, page, PM_PAGE_SIZE, sort),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, query, page, sort, manualRefreshKey],
  );
  const {
    data: usage,
    loading: usageLoading,
    error: usageError,
    reload: reloadUsage,
  } = useAsyncData(loadUsage, { silentKey: refreshKey });

  const loadTrend = useCallback(
    () => getTokenTrend(window, kind),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, kind, manualRefreshKey],
  );
  const {
    data: trend,
    loading: trendLoading,
    error: trendError,
    reload: reloadTrend,
  } = useAsyncData(loadTrend, { silentKey: refreshKey });

  const loadShare = useCallback(
    () => getModelShare(window),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [window, manualRefreshKey],
  );
  const {
    data: share,
    loading: shareLoading,
    error: shareError,
    reload: reloadShare,
  } = useAsyncData(loadShare, { silentKey: refreshKey });

  const kpiItems: PmKpiCard[] = useMemo(() => {
    if (!stats) return [];
    return [
      {
        key: 'modelCalls',
        label: t('platformMonitor.token.kpi.modelCalls'),
        value: fmtCompact(stats.modelCalls),
      },
      {
        key: 'totalTokens',
        label: t('platformMonitor.token.kpi.totalTokens'),
        value: fmtCompact(stats.totalTokens),
      },
      {
        key: 'avgTokens',
        label: t('platformMonitor.token.kpi.avgTokens'),
        value: stats.avgTokensPerCall == null ? '-' : fmtCompact(stats.avgTokensPerCall),
      },
      {
        key: 'modelCount',
        label: t('platformMonitor.token.kpi.modelCount'),
        value: fmtCount(stats.modelCount),
      },
      {
        // 「用量完整率」不是第 5 个平级的量，而是**解释**前四张卡为什么可能偏小：
        // 没回传用量的调用压根没有 token 数。放同一排、文案带小字说明
        key: 'completeness',
        label: t('platformMonitor.token.kpi.completeness'),
        value: fmtPercent1OrDash(stats.completenessRate),
      },
    ];
  }, [stats, t]);

  const columns = useMemo<TableColumnsType<PmModelUsageRow>>(() => {
    /**
     * 列 key 直接用 K4 的 `sort_by` 值，表格 onChange 里的 `columnKey` 才能反查出排序字段。
     *
     * sorter 交给后端：`sorter: true` 只出排序箭头、不做本地比较，
     * 否则「排了序的当前页」与跨页真正的序对不上。`sortOrder` 只给当前列，
     * 后端一次只接受一个排序字段，多列同时亮箭头会让人以为支持多列排序。
     */
    const sortable = (field: PmModelUsageSortField) => ({
      key: field,
      sorter: true,
      // 表头点击只有升 / 降两态：antd 默认第三态会「取消排序」并把回调里的 columnKey 抹掉，
      // 点起来就像第三下失效。这里关掉取消态，并用 onClickCapture 记住点的是哪一列
      sortDirections: ['ascend', 'descend'] as ('ascend' | 'descend')[],
      onHeaderCell: () => ({
        onClickCapture: () => {
          lastSortClickRef.current = field;
        },
      }),
      sortOrder: (!sortCleared && sort.field === field ? (sort.order === 'asc' ? 'ascend' : 'descend') : null) as
        'ascend' | 'descend' | null,
      showSorterTooltip: false,
    });

    return [
      {
        title: t('platformMonitor.token.column.model'),
        dataIndex: 'name',
        ...sortable('model'),
        render: (value: string, row) => (
          <div className="yx-pm-model-cell">
            <span className="yx-pm-cell-strong">{value}</span>
            {/* 后端 model_name 与 model_id 同源，相同时再渲染一行副标题只是重复 */}
            {row.modelId !== value && <span className="yx-pm-cell-sub">{row.modelId}</span>}
          </div>
        ),
      },
      {
        title: t('platformMonitor.token.column.calls'),
        dataIndex: 'calls',
        align: 'right',
        render: fmtCount,
        ...sortable('call_count'),
      },
      {
        title: t('platformMonitor.token.column.inputTokens'),
        dataIndex: 'inputTokens',
        align: 'right',
        render: fmtCompact,
        ...sortable('prompt_tokens'),
      },
      {
        title: t('platformMonitor.token.column.outputTokens'),
        dataIndex: 'outputTokens',
        align: 'right',
        render: fmtCompact,
        ...sortable('completion_tokens'),
      },
      {
        title: t('platformMonitor.token.column.totalTokens'),
        dataIndex: 'totalTokens',
        align: 'right',
        render: fmtCompact,
        ...sortable('total_tokens'),
      },
      {
        // 与 KPI 里的完整率同一件事的逐行版：这行有多少调用没回传用量
        title: t('platformMonitor.token.column.unmetered'),
        dataIndex: 'unmeteredCount',
        align: 'right',
        render: fmtCount,
      },
      {
        title: t('platformMonitor.token.column.avgTokens'),
        dataIndex: 'avgTokensPerCall',
        align: 'right',
        render: (value: number | null) => (value == null ? '-' : fmtCount(value)),
        ...sortable('avg_tokens_per_call'),
      },
      {
        title: t('platformMonitor.token.column.lastCallAt'),
        dataIndex: 'lastCallAt',
        ...sortable('last_called_at'),
      },
    ];
  }, [t, sort.field, sort.order, sortCleared]);

  return (
    <div className="yx-pm-pane">
      {/* 降级不是「没数据」：统计卡会照常出空壳，挂提示而不是走错误态 */}
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
        <StatCards items={kpiItems} loading={statsLoading} skeletonCount={5} />
      )}

      <div className="yx-pm-two-col yx-pm-two-col--wide-left">
        <SectionCard
          title={t('platformMonitor.token.trend')}
          extra={
            <SegmentedFilter
              value={kind}
              onChange={setKind}
              options={TREND_KINDS.map((item) => ({ value: item.value, label: t(item.labelKey) }))}
            />
          }
        >
          {trendError ? (
            <ModuleError error={trendError} onRetry={reloadTrend} />
          ) : (
            /* fill：同排的「模型使用分布」卡是这排的高者，图要跟着它撑满（见规格 §10） */
            <TrendAreaChart
              data={trend ?? []}
              loading={trendLoading}
              fill
              yFormat={fmtCompact}
              // 浮框固定四行、固定顺序：调用次数 → 输入 → 输出 → 总量。
              // 关掉「当前指标」那一行，否则第一行会跟着所选指标漂移、与 detail 里的同名字段重复
              showValueRow={false}
              // K2 一次给四个量，图只画一个；其余三个放进 tooltip，免得为此再发一次请求
              detailFields={[
                { key: 'calls', name: t('platformMonitor.token.kpi.modelCalls'), format: fmtCompact },
                { key: 'input', name: t('platformMonitor.token.kind.input'), format: fmtCompact },
                { key: 'output', name: t('platformMonitor.token.kind.output'), format: fmtCompact },
                { key: 'total', name: t('platformMonitor.token.kind.total'), format: fmtCompact },
              ]}
            />
          )}
        </SectionCard>

        <SectionCard title={t('platformMonitor.token.share')} hint={t('platformMonitor.token.shareHint')}>
          {shareError ? (
            <ModuleError error={shareError} onRetry={reloadShare} />
          ) : (
            <UsageBarList items={share ?? []} loading={shareLoading} />
          )}
        </SectionCard>
      </div>

      <SectionCard
        title={t('platformMonitor.token.models')}
        hint={t('platformMonitor.token.modelCount', { n: fmtCount(usage?.total ?? 0) })}
      >
        {/* 搜索框在标题行下方另起一行：设计稿里它与右上角的计数同排会挤在一起 */}
        <FilterBar>
          <Input
            size="small"
            allowClear
            className="yx-pm-search-input"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder={t('platformMonitor.token.searchModel')}
          />
        </FilterBar>
        {usageError ? (
          <ModuleError error={usageError} onRetry={reloadUsage} />
        ) : (
          <Table
            size="small"
            rowKey="modelId"
            loading={usageLoading}
            dataSource={usage?.rows ?? []}
            columns={columns}
            onChange={(_pagination, _filters, sorter, extra) => {
              // 翻页 / 筛选也会进这里，且 sorter 带着当前排序列；只有表头点击（action === 'sort'）才改排序
              const next = resolveUsageSortChange(extra.action, sorter, lastSortClickRef.current, sort, sortCleared);
              // capture 记下的列只服务「取消排序」那一下，用完即弃，避免后续无关回调误用
              lastSortClickRef.current = null;
              if (!next) return;
              setSort(next.sort);
              setSortCleared(next.cleared);
              setPage(1);
            }}
            pagination={{
              current: page,
              pageSize: PM_PAGE_SIZE,
              total: usage?.total ?? 0,
              onChange: setPage,
              showSizeChanger: false,
              size: 'small',
            }}
            scroll={{ x: 1320 }}
          />
        )}
      </SectionCard>
    </div>
  );
}
