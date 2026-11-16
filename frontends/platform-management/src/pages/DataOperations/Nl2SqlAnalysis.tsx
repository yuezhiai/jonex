import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Empty, Flex, Select, Switch } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  getNl2SqlOverview,
  listKbOptions,
  listNl2SqlFailures,
  listNl2SqlSlow,
  type DoNl2SqlQuery,
} from '@/api/dataOperations';
import useAsyncData from '@/hooks/useAsyncData';
import useCurrentSpace from '@/hooks/useCurrentSpace';
import { DO_DEFAULT_WINDOW, DO_KB_ALL } from '../../constants/dataOperations';
import KbSelect from './components/KbSelect';
import MultiTrendChart, { type MultiTrendSeries } from './components/MultiTrendChart';
import Nl2SqlFailurePanel from './components/Nl2SqlFailurePanel';
import Nl2SqlSlowTable from './components/Nl2SqlSlowTable';
import Nl2SqlSourceTable from './components/Nl2SqlSourceTable';
import SectionCard from './components/SectionCard';
import StatCards, { type StatCardItem } from './components/StatCards';
import WindowRangePicker from './components/WindowRangePicker';
import Nl2SqlDetailDrawer from './drawers/Nl2SqlDetailDrawer';
import { DO_NL2SQL_UNASSIGNED_SOURCE } from './types';
import type { DoKbOption, DoNl2SqlRow, DoNl2SqlSourceStats, DoWindow } from './types';
import { fmtCount, fmtDuration, fmtRate } from './utils';
import './index.scss';

/** 慢阈值下拉（毫秒）。默认 5s，与设计 §11 的「BIRD p50 约 3s」量级一致 */
const SLOW_OPTIONS_MS = [1000, 3000, 5000, 10000] as const;
const DEFAULT_SLOW_MS = 5000;

/** 慢查询/失败两张表统一的服务端分页大小（与列表端点默认值一致） */
const LIST_PAGE_SIZE = 10;

/** P95 样本数护栏：少于 20 条时不给具体数值，避免小样本被当成结论 */
const MIN_SAMPLES = 20;

/**
 * 数据运营 · 数据问答（NL→SQL 观测）页壳。
 *
 * 本页只做筛选条 + 6 张 KPI + 2 张趋势；慢查询表、失败复盘与详情抽屉在 Task 7。
 * 所有块同源于 `nl2sql-overview`，因此共用一次请求的 loading/error，
 * 但每块各自处理空态（KPI 仍成卡、趋势卡各自空态）。
 */
export default function Nl2SqlAnalysis() {
  const { t } = useTranslation();
  const spaceId = useCurrentSpace();

  const [window, setWindow] = useState<DoWindow>(DO_DEFAULT_WINDOW);
  const [kbId, setKbId] = useState<string>(DO_KB_ALL);
  const [dataSourceId, setDataSourceId] = useState('');
  const [slowMs, setSlowMs] = useState<number>(DEFAULT_SLOW_MS);
  const [includeSynthetic, setIncludeSynthetic] = useState(false);

  // 慢查询表与失败复盘各自分页；失败码过滤只作用于失败明细（分布始终是全量口径）
  const [slowPage, setSlowPage] = useState(1);
  const [failurePage, setFailurePage] = useState(1);
  const [reasonCode, setReasonCode] = useState<string | null>(null);

  // 正在查看的问答现场（点任意一张表的行打开抽屉）
  const [detailTarget, setDetailTarget] = useState<{ id: string; kbId: string | null } | null>(null);

  const [activeSpace, setActiveSpace] = useState(spaceId);

  /** 知识库下拉与当前空间联动；取不到时退空列表，不阻塞页面 */
  const [kbOptions, setKbOptions] = useState<DoKbOption[]>([]);
  const [kbLoading, setKbLoading] = useState(false);

  /**
   * 数据源下拉从上一次「未按源过滤」的 by_source 生成。
   * 一旦按源过滤，响应里的 by_source 只剩该源，直接回写会把其他选项从下拉里抹掉。
   */
  const [sourceOptions, setSourceOptions] = useState<DoNl2SqlSourceStats[]>([]);

  /**
   * 切换领域空间是外部来源的变化：在提交前同步清掉上一个空间的知识库/数据源筛选。
   * 用 effect 清理会先让一次取数带着旧 id 打到新空间；渲染期调整 state 则不会。
   */
  if (activeSpace !== spaceId) {
    setActiveSpace(spaceId);
    setKbId(DO_KB_ALL);
    setDataSourceId('');
    setSourceOptions([]);
    // 分页/失败码/抽屉都属于旧空间的行，切空间后必须先归零再取数
    setSlowPage(1);
    setFailurePage(1);
    setReasonCode(null);
    setDetailTarget(null);
  }

  // 选项尚未加载 / 切空间后旧选中项可能已失效：派生回「全部知识库」，
  // 不用 effect 改 state（与 DataAssets 的 effectiveKb 同模式）。
  const effectiveKbId = kbOptions.some((kb) => kb.id === kbId) ? kbId : DO_KB_ALL;

  useEffect(() => {
    let cancelled = false;
    setKbLoading(true);
    listKbOptions(spaceId)
      .then((options) => {
        if (!cancelled) setKbOptions(options);
      })
      .catch(() => {
        if (!cancelled) setKbOptions([]);
      })
      .finally(() => {
        if (!cancelled) setKbLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [spaceId]);

  const query: DoNl2SqlQuery = useMemo(
    () => ({ kbId: effectiveKbId, dataSourceId, slowMs, includeSynthetic }),
    [effectiveKbId, dataSourceId, slowMs, includeSynthetic],
  );

  // 把 spaceId 一起带回结果：切空间后 useAsyncData 会短暂保留上一份 data，
  // 若不按空间过滤，旧空间的 KPI/趋势会闪一帧，甚至把旧 by_source 写回数据源下拉。
  const loadOverview = useCallback(
    async () => ({ spaceId, data: await getNl2SqlOverview(spaceId, window, query) }),
    [spaceId, window, query],
  );
  const { data: overviewState, loading, error, reload } = useAsyncData(loadOverview);
  const overview = overviewState?.spaceId === spaceId ? overviewState.data : null;

  useEffect(() => {
    if (overview && !dataSourceId) setSourceOptions(overview.by_source);
  }, [overview, dataSourceId]);

  /**
   * 两张列表共用的收窄条件。失败码/分页是各自的，不放进这里。
   * ⚠️ 失败列表**不能带 slowMs**：failures 路由不接收 `slow_ms` / `min_ms`。
   */
  const listQuery = useMemo(
    () => ({ kbId: effectiveKbId, dataSourceId, includeSynthetic }),
    [effectiveKbId, dataSourceId, includeSynthetic],
  );

  // 与 overview 同款：把 spaceId 带回结果，切空间后不让上一空间的行闪一帧
  const loadSlow = useCallback(
    async () => ({
      spaceId,
      data: await listNl2SqlSlow(spaceId, window, {
        ...listQuery,
        slowMs,
        page: slowPage,
        pageSize: LIST_PAGE_SIZE,
      }),
    }),
    [spaceId, window, listQuery, slowMs, slowPage],
  );
  const {
    data: slowState,
    loading: slowLoading,
    error: slowError,
    reload: reloadSlow,
  } = useAsyncData(loadSlow);
  const slowList = slowState?.spaceId === spaceId ? slowState.data : null;

  const loadFailures = useCallback(
    async () => ({
      spaceId,
      data: await listNl2SqlFailures(spaceId, window, {
        ...listQuery,
        page: failurePage,
        pageSize: LIST_PAGE_SIZE,
        ...(reasonCode ? { reasonCode } : {}),
      }),
    }),
    [spaceId, window, listQuery, failurePage, reasonCode],
  );
  const {
    data: failureState,
    loading: failureLoading,
    error: failureError,
    reload: reloadFailures,
  } = useAsyncData(loadFailures);
  const failureList = failureState?.spaceId === spaceId ? failureState.data : null;

  const handleKbChange = (value: string) => {
    setKbId(value);
    // 数据源属于知识库，换库后旧的源筛选不再成立
    setDataSourceId('');
    setSlowPage(1);
    setFailurePage(1);
    setReasonCode(null);
  };

  const handleSourceChange = (value: string) => {
    setDataSourceId(value);
    setSlowPage(1);
    setFailurePage(1);
    setReasonCode(null);
  };

  const handleWindowChange = (value: DoWindow) => {
    setWindow(value);
    setSlowPage(1);
    setFailurePage(1);
  };

  const handleSlowMsChange = (value: number) => {
    setSlowMs(value);
    // 慢阈值只影响 overview 与慢查询列表；失败列表不重取，但分页仍归零以免旧页越界
    setSlowPage(1);
  };

  const handleIncludeSyntheticChange = (value: boolean) => {
    setIncludeSynthetic(value);
    setSlowPage(1);
    setFailurePage(1);
  };

  const handleReasonChange = (value: string | null) => {
    setReasonCode(value);
    setFailurePage(1);
  };

  const handleOpenDetail = (row: DoNl2SqlRow) => setDetailTarget({ id: row.id, kbId: row.knowledge_base_id });

  /** 样本不足时统一显示「—（样本不足）」，不暴露看似精确的 P95 */
  const p95Text = (value: number | null, samples: number) =>
    samples < MIN_SAMPLES ? t('dataOperations.nl2sql.kpi.insufficientSamples') : fmtDuration(value);

  const kpiItems: StatCardItem[] = useMemo(() => {
    if (!overview) return [];
    const { totals, latency, slow, precompile } = overview;
    return [
      {
        key: 'requests',
        label: t('dataOperations.nl2sql.kpi.requests'),
        value: fmtCount(totals.requests),
        accent: '#3b82f6',
        iconKey: 'search',
      },
      {
        key: 'successRate',
        label: t('dataOperations.nl2sql.kpi.successRate'),
        // 分母为 0 时后端给 null；成功率没有定义，不能伪造成 0%
        value: totals.success_rate == null ? '—' : fmtRate(totals.success_rate * 100),
        accent: '#10b981',
        iconKey: 'percent',
      },
      {
        key: 'sqlP95',
        label: t('dataOperations.nl2sql.kpi.sqlP95'),
        value: p95Text(latency.sql_p95_ms, latency.sql_samples),
        accent: '#8b5cf6',
        iconKey: 'avg',
      },
      {
        key: 'totalP95',
        label: t('dataOperations.nl2sql.kpi.totalP95'),
        value: p95Text(latency.total_p95_ms, latency.total_samples),
        accent: '#f59e0b',
        iconKey: 'avg',
      },
      {
        key: 'slowQueries',
        label: t('dataOperations.nl2sql.kpi.slowQueries'),
        value: fmtCount(slow.sql_count),
        delta: fmtRate(slow.sql_rate * 100),
        accent: '#ef4444',
        iconKey: 'database',
      },
      {
        key: 'retryRate',
        label: t('dataOperations.nl2sql.kpi.retryRate'),
        value: fmtRate(precompile.retry_rate * 100),
        accent: '#0ea5e9',
        iconKey: 'relation',
      },
    ];
  }, [overview, t]);

  const volumeSeries: MultiTrendSeries[] = useMemo(() => {
    const points = overview?.trend ?? [];
    return [
      {
        key: 'requests',
        name: t('dataOperations.nl2sql.trend.requests'),
        color: '#3b82f6',
        points: points.map((point) => ({ date: point.date, value: point.requests })),
      },
      {
        key: 'failed',
        name: t('dataOperations.nl2sql.trend.failed'),
        color: '#ef4444',
        points: points.map((point) => ({ date: point.date, value: point.failed })),
      },
    ];
  }, [overview, t]);

  const p95Series: MultiTrendSeries[] = useMemo(() => {
    const points = overview?.trend ?? [];
    return [
      {
        key: 'sqlP95',
        name: t('dataOperations.nl2sql.trend.sqlP95'),
        color: '#8b5cf6',
        points: points.map((point) => ({
          date: point.date,
          value: point.sql_p95_ms,
          lowSample: point.sql_samples < MIN_SAMPLES,
        })),
      },
      {
        key: 'totalP95',
        name: t('dataOperations.nl2sql.trend.totalP95'),
        color: '#f59e0b',
        points: points.map((point) => ({
          date: point.date,
          value: point.total_p95_ms,
          lowSample: point.total_samples < MIN_SAMPLES,
        })),
      },
    ];
  }, [overview, t]);

  // 后端趋势会按天补零，trend 永远非空 —— 判空只能看 totals.requests。
  // 再兜一道「趋势里确实没有任何非零量」，避免 totals 与 trend 口径临时不一致时误判。
  const isEmptyWindow =
    overview != null &&
    overview.totals.requests === 0 &&
    overview.trend.every((point) => point.requests === 0 && point.failed === 0);
  const noObservations = (
    <Flex align="center" justify="center" style={{ height: 260 }}>
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={t('dataOperations.nl2sql.empty.noObservations')}
      />
    </Flex>
  );

  // KPI 与趋势同源于 overview：失败时每块各自给出可重试的提示，而不是整页白屏。
  // 已有上一份数据时保留旧图（重取失败不等于数据作废）。
  const errorAlert = error ? (
    <Alert
      type="error"
      showIcon
      style={{ marginBottom: 16 }}
      message={t('dataOperations.error.loadFailed')}
      description={error.message}
      action={
        <Button size="small" onClick={reload}>
          {t('dataOperations.common.retry')}
        </Button>
      }
    />
  ) : null;

  /** 单块失败：只影响该块，给出可重试提示（不拖垮整页） */
  const blockError = (err: Error, retry: () => void) => (
    <Alert
      type="error"
      showIcon
      style={{ marginBottom: 12 }}
      message={t('dataOperations.error.loadFailed')}
      description={err.message}
      action={
        <Button size="small" onClick={retry}>
          {t('dataOperations.common.retry')}
        </Button>
      }
    />
  );

  return (
    <>
      <Flex wrap="wrap" gap={12} align="center" style={{ marginBottom: 16 }}>
        <WindowRangePicker value={window} onChange={handleWindowChange} />
        <KbSelect value={effectiveKbId} options={kbOptions} loading={kbLoading} onChange={handleKbChange} />
        <Select
          size="small"
          style={{ minWidth: 160 }}
          value={dataSourceId}
          onChange={handleSourceChange}
          options={[
            { value: '', label: t('dataOperations.nl2sql.filter.allSource') },
            ...sourceOptions.map((source) => ({
              value: source.data_source_id,
              label:
                source.data_source_id === DO_NL2SQL_UNASSIGNED_SOURCE
                  ? t('dataOperations.nl2sql.filter.unassignedSource')
                  : source.source_name || source.data_source_id,
            })),
          ]}
        />
        <Select
          size="small"
          style={{ width: 120 }}
          value={slowMs}
          onChange={handleSlowMsChange}
          options={SLOW_OPTIONS_MS.map((ms) => ({
            value: ms,
            label: t('dataOperations.nl2sql.filter.slowOption', { seconds: ms / 1000 }),
          }))}
        />
        <Flex align="center" gap={6}>
          <Switch size="small" checked={includeSynthetic} onChange={handleIncludeSyntheticChange} />
          <span>{t('dataOperations.nl2sql.filter.includeSynthetic')}</span>
        </Flex>
      </Flex>

      {error && overview ? errorAlert : null}
      {error && !overview ? (
        errorAlert
      ) : (
        <StatCards items={kpiItems} loading={loading && !overview} skeletonCount={6} />
      )}

      <div className="yx-do-two-col">
        <SectionCard title={t('dataOperations.nl2sql.section.volumeTrendTitle')}>
          {error && !overview ? (
            errorAlert
          ) : isEmptyWindow ? (
            noObservations
          ) : (
            <MultiTrendChart
              series={volumeSeries}
              loading={loading}
              emptyText={t('dataOperations.nl2sql.empty.noObservations')}
            />
          )}
        </SectionCard>

        <SectionCard
          title={t('dataOperations.nl2sql.section.p95TrendTitle')}
          description={t('dataOperations.nl2sql.section.p95TrendDesc')}
        >
          {error && !overview ? (
            errorAlert
          ) : isEmptyWindow ? (
            noObservations
          ) : (
            <MultiTrendChart
              series={p95Series}
              loading={loading}
              emptyText={t('dataOperations.nl2sql.empty.noLatencySamples')}
              formatValue={fmtDuration}
            />
          )}
        </SectionCard>
      </div>

      <div className="yx-do-two-col yx-do-two-col--stack">
        <SectionCard title={t('dataOperations.nl2sql.section.slowTitle')}>
          {slowError ? (
            blockError(slowError, reloadSlow)
          ) : (
            <Nl2SqlSlowTable
              rows={slowList?.items ?? []}
              total={slowList?.total ?? 0}
              loading={slowLoading}
              page={slowPage}
              pageSize={LIST_PAGE_SIZE}
              onPageChange={setSlowPage}
              onOpen={handleOpenDetail}
            />
          )}
        </SectionCard>

        <SectionCard title={t('dataOperations.nl2sql.section.failureTitle')}>
          {failureError ? (
            blockError(failureError, reloadFailures)
          ) : (
            <Nl2SqlFailurePanel
              rows={failureList?.items ?? []}
              total={failureList?.total ?? 0}
              // 分布始终取 overview 的全量口径，不随失败码筛选收缩
              reasons={overview?.reason_codes ?? []}
              loading={failureLoading}
              reasonCode={reasonCode}
              onReasonChange={handleReasonChange}
              onOpen={handleOpenDetail}
              page={failurePage}
              pageSize={LIST_PAGE_SIZE}
              onPageChange={setFailurePage}
            />
          )}
        </SectionCard>
      </div>

      <SectionCard title={t('dataOperations.nl2sql.section.sourceTitle')}>
        {error && !overview ? (
          errorAlert
        ) : (
          <Nl2SqlSourceTable
            // 用「未按源过滤」时缓存的那份：按源过滤后 overview.by_source
            // 只剩当前源，直接用会把其他行（含未识别源）从表里抹掉。
            sources={sourceOptions}
            loading={loading}
            activeSourceId={dataSourceId}
            onSourceChange={handleSourceChange}
          />
        )}
      </SectionCard>

      <Nl2SqlDetailDrawer
        id={detailTarget?.id ?? null}
        open={detailTarget != null}
        onClose={() => setDetailTarget(null)}
        spaceId={spaceId}
        kbId={detailTarget?.kbId ?? null}
      />
    </>
  );
}
