import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Result, Table } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  getKnowledgeKpis,
  getKnowledgeOverview,
  getKnowledgeTrend,
  listHotInstances,
  listIdleKnowledge,
} from '@/api/dataOperations';
import useCurrentSpace from '@/hooks/useCurrentSpace';
import useAsyncData from '@/hooks/useAsyncData';
import SectionCard from './components/SectionCard';
import StatCards, { type StatCardItem } from './components/StatCards';
import TrendAreaChart from './components/TrendAreaChart';
import ProportionList from './components/ProportionList';
import SegmentedFilter from './components/SegmentedFilter';
import WindowRangePicker from './components/WindowRangePicker';
import EntityDetailDrawer from './drawers/EntityDetailDrawer';
import { DO_DEFAULT_WINDOW } from '../../constants/dataOperations';
import type { DoEntityTarget, DoHotInstance, DoIdleKnowledge, DoKnowledgeKpi, DoScope, DoWindow } from './types';
import './index.scss';

type KnowledgeOverview = Awaited<ReturnType<typeof getKnowledgeOverview>>;
type IdleWindow = 30 | 90;

export default function KnowledgeAssets() {
  const { t } = useTranslation();
  // 切换领域空间 → 知识侧 KPI、趋势、分布、活跃度全部按成员库重算
  const spaceId = useCurrentSpace();

  const [scope, setScope] = useState<DoScope>('ontology');
  const [window, setWindow] = useState<DoWindow>(DO_DEFAULT_WINDOW);
  const [idleWindow, setIdleWindow] = useState<IdleWindow>(30);

  const [kpis, setKpis] = useState<DoKnowledgeKpi[]>([]);
  const [kpiLoading, setKpiLoading] = useState(true);
  const [overview, setOverview] = useState<KnowledgeOverview | null>(null);
  const [scopeLoading, setScopeLoading] = useState(true);
  const [hotInstances, setHotInstances] = useState<DoHotInstance[]>([]);
  const [hotLoading, setHotLoading] = useState(true);
  const [idleKnowledge, setIdleKnowledge] = useState<DoIdleKnowledge[]>([]);
  const [idleLoading, setIdleLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 正在查看的本体对象（点列表里的名称打开抽屉 B）
  const [entityTarget, setEntityTarget] = useState<DoEntityTarget | null>(null);

  const loadKpis = useCallback(async () => {
    setKpiLoading(true);
    setError(null);
    try {
      setKpis(await getKnowledgeKpis(spaceId));
    } catch (e) {
      setError(e instanceof Error ? e.message : t('dataOperations.error.loadFailed'));
    } finally {
      setKpiLoading(false);
    }
  }, [t, spaceId]);

  // 分析对象切换只影响「分布 + 活跃度」，KPI 行不参与，避免无谓的闪烁
  const loadByScope = useCallback(async () => {
    setScopeLoading(true);
    try {
      setOverview(await getKnowledgeOverview(scope, spaceId));
    } catch {
      setOverview(null);
    } finally {
      setScopeLoading(false);
    }
  }, [scope, spaceId]);

  // 高频实例表随区间变化（「命中次数」就是在这个窗口里算的），所以从 loadByScope 里拆出来，
  // 让切区间只重取这一块而不是整排（分布与活跃度维持近 7 天）
  const loadHot = useCallback(async () => {
    setHotLoading(true);
    try {
      setHotInstances(await listHotInstances(scope, spaceId, window));
    } catch {
      setHotInstances([]);
    } finally {
      setHotLoading(false);
    }
  }, [scope, spaceId, window]);

  useEffect(() => {
    void loadKpis();
  }, [loadKpis]);

  useEffect(() => {
    void loadByScope();
  }, [loadByScope]);

  useEffect(() => {
    void loadHot();
  }, [loadHot]);

  useEffect(() => {
    let cancelled = false;
    setIdleLoading(true);
    listIdleKnowledge(scope, idleWindow, spaceId, window)
      .then((list) => {
        if (!cancelled) setIdleKnowledge(list);
      })
      .catch(() => {
        if (!cancelled) setIdleKnowledge([]);
      })
      .finally(() => {
        if (!cancelled) setIdleLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scope, idleWindow, spaceId, window]);

  const kpiItems: StatCardItem[] = useMemo(
    () =>
      kpis.map((kpi) => ({
        key: kpi.key,
        label: t(kpi.labelKey),
        value: kpi.value,
        delta: (
          <>
            {t('dataOperations.kpi.newIn7d')} {kpi.deltaText}
          </>
        ),
        accent: kpi.accent,
        iconKey: kpi.iconKey,
      })),
    [kpis, t],
  );

  // 增长趋势的窗口是卡片自己的事：切区间只重取趋势、高频实例与未使用知识三块，
  // KPI、分布、活跃度都还按近 7 天
  const loadTrend = useCallback(() => getKnowledgeTrend(scope, spaceId, window), [scope, spaceId, window]);
  const { data: trend, loading: trendLoading } = useAsyncData(loadTrend);

  /**
   * 点名称进抽屉 B。定位键是**三元组**（kbId + 类型 + 规范名），不是行上的 `id`
   * —— 那个 `object_id` 是单向哈希，反解不回三元组、也查不了详情。
   * `kbId` 富化不到时没有可用的定位键，退回纯文本（后端也拦不住串库查询）。
   */
  const renderEntityName = (row: DoHotInstance | DoIdleKnowledge) => {
    const kbId = row.kbId;
    if (!kbId) return <strong>{row.name}</strong>;
    return (
      <a
        className="yx-do-link"
        onClick={() => setEntityTarget({ kbId, entityType: row.objectType, canonicalName: row.name })}
      >
        {row.name}
      </a>
    );
  };

  const hotColumns = [
    {
      title: t('dataOperations.column.instanceName'),
      dataIndex: 'name',
      key: 'name',
      render: (_: string, row: DoHotInstance) => renderEntityName(row),
    },
    { title: t('dataOperations.column.objectType'), dataIndex: 'objectType', key: 'objectType', width: 150 },
    { title: t('dataOperations.column.kbName'), dataIndex: 'kbName', key: 'kbName', width: 180 },
    {
      title: t('dataOperations.column.hitCount'),
      dataIndex: 'hitCount',
      key: 'hitCount',
      width: 110,
      align: 'right' as const,
      render: (v: number) => <span className="yx-do-num">{v.toLocaleString()}</span>,
    },
    {
      title: t('dataOperations.column.adoptCount'),
      dataIndex: 'adoptCount',
      key: 'adoptCount',
      width: 110,
      align: 'right' as const,
      render: (v: number) => <span className="yx-do-num">{v.toLocaleString()}</span>,
    },
    {
      title: t('dataOperations.column.lastUsedAt'),
      dataIndex: 'lastUsedAt',
      key: 'lastUsedAt',
      width: 170,
    },
  ];

  const idleColumns = [
    {
      title: t('dataOperations.column.instanceName'),
      dataIndex: 'name',
      key: 'name',
      render: (_: string, row: DoIdleKnowledge) => renderEntityName(row),
    },
    { title: t('dataOperations.column.objectType'), dataIndex: 'objectType', key: 'objectType', width: 150 },
    { title: t('dataOperations.column.kbName'), dataIndex: 'kbName', key: 'kbName', width: 180 },
    {
      title: t('dataOperations.column.lastUsedAt'),
      dataIndex: 'lastUsedAt',
      key: 'lastUsedAt',
      width: 170,
      render: (v: string | null) => v ?? <span className="yx-do-muted">{t('dataOperations.common.neverUsed')}</span>,
    },
    {
      title: t('dataOperations.column.updatedAt'),
      dataIndex: 'updatedAt',
      key: 'updatedAt',
      width: 170,
    },
  ];

  if (error) {
    return (
      <Result
        status="error"
        title={t('dataOperations.error.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={() => void loadKpis()}>
            {t('dataOperations.common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <StatCards items={kpiItems} loading={kpiLoading} />

      <div className="yx-card yx-do-scope-bar">
        <div className="yx-do-scope-left">
          <span className="yx-do-scope-label">{t('dataOperations.scope.label')}</span>
          <SegmentedFilter
            size="middle"
            value={scope}
            onChange={setScope}
            options={[
              { value: 'ontology', label: t('dataOperations.scope.ontology') },
              { value: 'wiki', label: t('dataOperations.scope.wiki') },
            ]}
          />
        </div>
        <div className="yx-do-scope-metrics">
          <span className="yx-do-scope-metric">
            {t('dataOperations.scope.activeInstances')}
            <em>{(overview?.activity.activeCount ?? 0).toLocaleString()}</em>
          </span>
          <span className="yx-do-scope-metric">
            {t('dataOperations.scope.activityRate')}
            <em>{(overview?.activity.activeRate ?? 0).toFixed(2)}%</em>
          </span>
        </div>
      </div>

      <div className="yx-do-two-col">
        <SectionCard
          title={t('dataOperations.section.growthTitle')}
          extra={<WindowRangePicker value={window} onChange={setWindow} />}
        >
          <TrendAreaChart
            data={trend ?? []}
            loading={trendLoading}
            color="#8b5cf6"
            valueName={t('dataOperations.column.count')}
          />
        </SectionCard>

        <SectionCard
          title={scope === 'wiki' ? t('dataOperations.section.distWikiTitle') : t('dataOperations.section.distTitle')}
        >
          <ProportionList items={overview?.dist ?? []} loading={scopeLoading} />
        </SectionCard>
      </div>

      <SectionCard
        title={
          scope === 'wiki'
            ? t('dataOperations.section.hotWikiInstancesTitle')
            : t('dataOperations.section.hotInstancesTitle')
        }
        hint={t('dataOperations.section.hotInstancesHint')}
      >
        <Table
          rowKey="id"
          size="small"
          columns={hotColumns}
          dataSource={hotInstances}
          loading={hotLoading}
          pagination={false}
        />
      </SectionCard>

      <SectionCard
        title={t('dataOperations.section.idleKnowledgeTitle')}
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
          dataSource={idleKnowledge}
          loading={idleLoading}
          pagination={false}
        />
      </SectionCard>

      <EntityDetailDrawer
        target={entityTarget}
        spaceId={spaceId}
        window={window}
        onClose={() => setEntityTarget(null)}
      />
    </>
  );
}
