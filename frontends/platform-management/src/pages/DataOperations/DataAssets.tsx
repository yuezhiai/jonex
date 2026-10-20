import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Result, Table } from 'antd';
import { useTranslation } from 'react-i18next';
import { navigateCrossApp } from '@jonex/shell-sdk';
import {
  getAbnormalAssets,
  getAssetComposition,
  getAssetKpis,
  getAssetTrend,
  getPipeline,
  getQualityAnomalies,
  listHotAssets,
  listIdleAssets,
  listKbOptions,
} from '@/api/dataOperations';
import useAsyncData from '@/hooks/useAsyncData';
import useCurrentSpace from '@/hooks/useCurrentSpace';
import SectionCard from './components/SectionCard';
import StatCards, { type StatCardItem } from './components/StatCards';
import TrendAreaChart from './components/TrendAreaChart';
import ProportionList from './components/ProportionList';
import SegmentedFilter from './components/SegmentedFilter';
import AssetTypeTag from './components/AssetTypeTag';
import KbSelect from './components/KbSelect';
import WindowRangePicker from './components/WindowRangePicker';
import PipelineOverview from './components/PipelineOverview';
import AnomalySummary from './components/AnomalySummary';
import AssetDetailDrawer from './drawers/AssetDetailDrawer';
import { DO_DEFAULT_WINDOW, DO_KB_ALL } from '../../constants/dataOperations';
import type {
  DoAnomalyAsset,
  DoAnomalySummary,
  DoAssetTarget,
  DoAssetType,
  DoHotAsset,
  DoIdleAsset,
  DoKbOption,
  DoKpiCard,
  DoPipelineOverview,
  DoPipelineStats,
  DoQualityAnomaly,
  DoWindow,
} from './types';
import './index.scss';

type IdleWindow = 30 | 90;

export default function DataAssets() {
  const { t } = useTranslation();
  // 切换领域空间 → 整页按新空间的成员库重新取数
  const spaceId = useCurrentSpace();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [kpis, setKpis] = useState<DoKpiCard[]>([]);
  const [kbOptions, setKbOptions] = useState<DoKbOption[]>([]);
  const [pipeline, setPipeline] = useState<{ data: DoPipelineOverview; stats: DoPipelineStats } | null>(null);
  const [abnormal, setAbnormal] = useState<{ summary: DoAnomalySummary; latest: DoAnomalyAsset | null } | null>(null);
  const [qualityAnomalies, setQualityAnomalies] = useState<DoQualityAnomaly[]>([]);

  const [window, setWindow] = useState<DoWindow>(DO_DEFAULT_WINDOW);

  const [idleWindow, setIdleWindow] = useState<IdleWindow>(30);

  // 正在查看的资产（点列表里的名称打开抽屉 A）。状态放这里而不是抽屉内部：
  // 关闭时要能把它清空，且窗口/空间变化时抽屉自己会按新参数重取
  const [assetTarget, setAssetTarget] = useState<DoAssetTarget | null>(null);

  // 趋势与构成各有一个知识库筛选，切其中一张不牵动同排的另一张
  const [trendKb, setTrendKb] = useState(DO_KB_ALL);
  const [compKb, setCompKb] = useState(DO_KB_ALL);

  // 切空间后原选中库可能已不在选项内，派生回「全部知识库」。
  // 不用 effect 去改 state：那样会多一帧脏值、多触发一次取数
  const effectiveTrendKb = kbOptions.some((kb) => kb.id === trendKb) ? trendKb : DO_KB_ALL;
  const effectiveCompKb = kbOptions.some((kb) => kb.id === compKb) ? compKb : DO_KB_ALL;

  const loadMain = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 这四块同属 data-assets，取数层按 key 合并成一次请求，不是四个 HTTP 调用
      const [kpiList, options, pipelineData, abnormalData, qualityData] = await Promise.all([
        getAssetKpis(spaceId),
        listKbOptions(spaceId),
        getPipeline(spaceId),
        getAbnormalAssets(spaceId),
        getQualityAnomalies(spaceId),
      ]);
      setKpis(kpiList);
      setKbOptions(options);
      setPipeline(pipelineData);
      setAbnormal(abnormalData);
      setQualityAnomalies(qualityData);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('dataOperations.error.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t, spaceId]);

  useEffect(() => {
    void loadMain();
  }, [loadMain]);

  const loadTrend = useCallback(
    () => getAssetTrend(effectiveTrendKb, spaceId, window),
    [effectiveTrendKb, spaceId, window],
  );
  const loadComposition = useCallback(() => getAssetComposition(effectiveCompKb, spaceId), [effectiveCompKb, spaceId]);
  const loadIdle = useCallback(() => listIdleAssets(idleWindow, spaceId, window), [idleWindow, spaceId, window]);
  // 高频引用资产表随区间变化（它的「引用次数」就在这个窗口里算），所以不属于首屏那批。
  // 拉出来单独取数，切区间时只有它和趋势图回到骨架态，KPI 与流水维持近 7 天不动。
  const loadHot = useCallback(() => listHotAssets(spaceId, window), [spaceId, window]);

  // 这几块与首屏分开取数：切筛选器时只让所在卡片回到骨架态，不牵动整页
  const { data: trend, loading: trendLoading } = useAsyncData(loadTrend);
  const { data: composition, loading: compLoading } = useAsyncData(loadComposition);
  const { data: idleAssets, loading: idleLoading } = useAsyncData(loadIdle);
  const { data: hotAssets, loading: hotLoading } = useAsyncData(loadHot);

  const kpiItems: StatCardItem[] = useMemo(
    () =>
      kpis.map((kpi) => ({
        key: kpi.key,
        label: t(kpi.labelKey),
        value: kpi.value,
        accent: kpi.accent,
        iconKey: kpi.iconKey,
      })),
    [kpis, t],
  );

  // 按来源的构成契约里没有对应字段，「类型 / 来源」分段器与 compositionBySource 一并下线
  const compositionData = composition?.composition ?? [];

  const typeLabel = useCallback(
    (type: DoAssetType) => <AssetTypeTag type={type} label={t(`dataOperations.assetType.${type}`)} />,
    [t],
  );

  // 目标页归属 core-business，是另一个子应用：ShellContext.navigate 只会拼本应用的 basePath，
  // 改走 shell-sdk 的跨应用导航 —— hosted 下由 shell 的 SPA 路由切过去（文档不重载），
  // standalone / iframe fallback 下没人接管，SDK 内部回落到顶层窗口整页导航
  const goToKb = useCallback((kbId: string) => {
    navigateCrossApp(`/apps/core-business/domain-knowledge/${kbId}`);
  }, []);

  // 两张表共用同一种「前往知识库」：没有 kbId 就没有跳转目标（文档已删 / 跨租户，
  // 后端富化不到），置灰而不是点进空白页
  const renderKbAction = useCallback(
    (kbId: string | null) =>
      kbId ? (
        <a className="yx-do-link" onClick={() => goToKb(kbId)}>
          {t('dataOperations.common.goToKb')}
        </a>
      ) : (
        <span className="yx-do-link yx-do-link--disabled">{t('dataOperations.common.goToKb')}</span>
      ),
    [goToKb, t],
  );

  // 点名称进抽屉 A。带上本行的 kbId 做交叉校验：该资产若不属于这个库，后端一律回 404
  // （与「不存在 / 无权限」共用一个口径），比串库展示别的库的数据安全
  const openAsset = useCallback((id: string, kbId: string | null) => setAssetTarget({ assetId: id, kbId }), []);

  const hotColumns = [
    {
      title: t('dataOperations.column.assetName'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string, row: DoHotAsset) => (
        <a className="yx-do-link" onClick={() => openAsset(row.id, row.kbId)}>
          {v}
        </a>
      ),
    },
    {
      title: t('dataOperations.column.type'),
      dataIndex: 'assetType',
      key: 'assetType',
      width: 110,
      render: typeLabel,
    },
    { title: t('dataOperations.column.kbName'), dataIndex: 'kbName', key: 'kbName' },
    {
      title: t('dataOperations.column.refCount'),
      dataIndex: 'refCount',
      key: 'refCount',
      width: 110,
      align: 'right' as const,
      render: (v: number) => <span className="yx-do-num">{v.toLocaleString()}</span>,
    },
    {
      title: t('dataOperations.column.lastRefAt'),
      dataIndex: 'lastRefAt',
      key: 'lastRefAt',
      width: 170,
    },
    {
      title: t('dataOperations.column.action'),
      key: 'action',
      width: 120,
      render: (_: unknown, row: DoHotAsset) => renderKbAction(row.kbId),
    },
  ];

  const idleColumns = [
    {
      title: t('dataOperations.column.assetName'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string, row: DoIdleAsset) => (
        <a className="yx-do-link" onClick={() => openAsset(row.id, row.kbId)}>
          {v}
        </a>
      ),
    },
    { title: t('dataOperations.column.type'), dataIndex: 'assetType', key: 'assetType', width: 110, render: typeLabel },
    { title: t('dataOperations.column.kbName'), dataIndex: 'kbName', key: 'kbName' },
    {
      title: t('dataOperations.column.lastRefAt'),
      dataIndex: 'lastRefAt',
      key: 'lastRefAt',
      width: 170,
      render: (v: string | null) =>
        v ?? <span className="yx-do-muted">{t('dataOperations.common.neverReferenced')}</span>,
    },
    { title: t('dataOperations.column.updatedAt'), dataIndex: 'updatedAt', key: 'updatedAt', width: 170 },
    {
      title: t('dataOperations.column.action'),
      key: 'action',
      width: 120,
      render: (_: unknown, row: DoIdleAsset) => renderKbAction(row.kbId),
    },
  ];

  if (error) {
    return (
      <Result
        status="error"
        title={t('dataOperations.error.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={() => void loadMain()}>
            {t('dataOperations.common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <StatCards items={kpiItems} loading={loading} />

      <div className="yx-do-two-col yx-do-two-col--wide-right">
        <SectionCard
          title={t('dataOperations.section.trendTitle')}
          extra={
            <>
              <KbSelect value={effectiveTrendKb} options={kbOptions} onChange={setTrendKb} loading={loading} />
              {/* 区间同时驱动趋势、下面两张喂抽屉的表与抽屉本身 —— KPI 与构成图固定近 7 天 */}
              <WindowRangePicker value={window} onChange={setWindow} />
            </>
          }
        >
          {/* fill：本排两卡等高，右卡更高，不撑满的话趋势卡底部会空出一截 */}
          <TrendAreaChart
            data={trend ?? []}
            loading={trendLoading}
            valueName={t('dataOperations.column.ingested')}
            fill
          />
        </SectionCard>

        <SectionCard
          title={t('dataOperations.section.compositionTitle')}
          extra={<KbSelect value={effectiveCompKb} options={kbOptions} onChange={setCompKb} loading={loading} />}
        >
          {/* total 用后端全量口径：图上只列五类，隐藏的三类仍要在分母里 */}
          <ProportionList items={compositionData} total={composition?.total} loading={compLoading} />
        </SectionCard>
      </div>

      <SectionCard title={t('dataOperations.section.hotAssetsTitle')} hint={t('dataOperations.section.hotAssetsHint')}>
        <Table
          rowKey="id"
          size="small"
          columns={hotColumns}
          dataSource={hotAssets ?? []}
          loading={hotLoading}
          pagination={false}
        />
      </SectionCard>

      <SectionCard
        title={t('dataOperations.section.idleAssetsTitle')}
        extra={
          <SegmentedFilter
            value={idleWindow}
            onChange={setIdleWindow}
            options={[
              { value: 30, label: t('dataOperations.filter.idle30') },
              { value: 90, label: t('dataOperations.filter.idle90') },
            ]}
          />
        }
      >
        <Table
          rowKey="id"
          size="small"
          columns={idleColumns}
          dataSource={idleAssets ?? []}
          loading={idleLoading}
          pagination={false}
        />
      </SectionCard>

      <div className="yx-do-two-col-even">
        <SectionCard
          title={t('dataOperations.section.pipelineTitle')}
          description={t('dataOperations.section.pipelineDesc')}
        >
          <PipelineOverview data={pipeline?.data ?? null} stats={pipeline?.stats ?? null} loading={loading} />
        </SectionCard>

        <SectionCard title={t('dataOperations.section.anomalyTitle')}>
          <AnomalySummary summary={abnormal?.summary ?? null} latest={abnormal?.latest ?? null} loading={loading} />
        </SectionCard>
      </div>

      <SectionCard
        title={t('dataOperations.section.qualityAnomalyTitle')}
        description={t('dataOperations.section.qualityAnomalyDesc')}
      >
        <Table
          rowKey="documentId"
          size="small"
          loading={loading}
          dataSource={qualityAnomalies}
          pagination={false}
          locale={{ emptyText: t('dataOperations.empty.qualityAnomaly') }}
          columns={[
            {
              title: t('dataOperations.quality.document'),
              dataIndex: 'documentName',
              key: 'documentName',
              ellipsis: true,
            },
            {
              title: t('dataOperations.quality.kb'),
              dataIndex: 'kbName',
              key: 'kbName',
              width: 180,
              ellipsis: true,
            },
            {
              title: t('dataOperations.quality.reasons'),
              dataIndex: 'reasons',
              key: 'reasons',
              render: (reasons: string[]) =>
                (reasons || [])
                  .map((r) => t(`dataOperations.quality.reason.${r}`, { defaultValue: r }))
                  .join('；'),
            },
            {
              title: t('dataOperations.quality.occurredAt'),
              dataIndex: 'occurredAt',
              key: 'occurredAt',
              width: 170,
            },
          ]}
        />
      </SectionCard>

      <AssetDetailDrawer
        target={assetTarget}
        spaceId={spaceId}
        window={window}
        onClose={() => setAssetTarget(null)}
        onGoToKb={goToKb}
      />
    </>
  );
}
