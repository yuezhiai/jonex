import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Result, Table, Tag } from 'antd';
import dayjs from 'dayjs';
import { useTranslation } from 'react-i18next';
import {
  getQuestionTopics,
  getSearchOverview,
  getSearchTrend,
  listHotQuestions,
  listKbUsage,
  listServiceUsage,
} from '@/api/dataOperations';
import useCurrentSpace from '@/hooks/useCurrentSpace';
import useAsyncData from '@/hooks/useAsyncData';
import SectionCard from './components/SectionCard';
import StatCards, { type StatCardItem } from './components/StatCards';
import TrendAreaChart from './components/TrendAreaChart';
import ProportionList from './components/ProportionList';
import WindowRangePicker from './components/WindowRangePicker';
import KbDetailDrawer from './drawers/KbDetailDrawer';
import ServiceDetailDrawer from './drawers/ServiceDetailDrawer';
import { DO_DEFAULT_WINDOW } from '../../constants/dataOperations';
import type { DoHotQuestion, DoProportionItem, DoUsageItem, DoWindow } from './types';
import './index.scss';

type SearchOverview = Awaited<ReturnType<typeof getSearchOverview>>;

export default function SearchAnalysis() {
  const { t } = useTranslation();
  // 切换领域空间 → 检索侧 KPI、趋势、方式分布、两张使用表全部按成员库重算
  const spaceId = useCurrentSpace();

  const [window, setWindow] = useState<DoWindow>(DO_DEFAULT_WINDOW);
  const [overview, setOverview] = useState<SearchOverview | null>(null);
  const [hotQuestions, setHotQuestions] = useState<DoHotQuestion[]>([]);
  const [topics, setTopics] = useState<DoProportionItem[]>([]);
  const [kbUsage, setKbUsage] = useState<DoUsageItem[]>([]);
  const [usageLoading, setUsageLoading] = useState(true);
  const [serviceUsage, setServiceUsage] = useState<DoUsageItem[]>([]);
  /** 高频提问卡上「数据更新至 X」的那个 X（MM-DD HH:mm），见下方注释 */
  const [fetchedAt, setFetchedAt] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 正在查看的知识库 / 领域服务（点两张使用表里的名称，分别打开抽屉 C 与 D）
  const [kbTarget, setKbTarget] = useState<string | null>(null);
  const [serviceTarget, setServiceTarget] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [ov, hot, tp] = await Promise.all([
        getSearchOverview(spaceId),
        listHotQuestions(spaceId),
        getQuestionTopics(spaceId),
      ]);
      setOverview(ov);
      setHotQuestions(hot);
      setTopics(tp);
      // 「数据更新至」取的是这批数据落地的那一刻：后端没有更新时间字段，几个数字都是查的时候现算的，
      // 所以数据的新鲜度就等于这次请求完成的时刻 —— 不再拿一个写死的常量假装是更新时间
      setFetchedAt(dayjs().format('MM-DD HH:mm'));
    } catch (e) {
      setError(e instanceof Error ? e.message : t('dataOperations.error.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t, spaceId]);

  // 两张使用表随区间变化（表里的「检索次数」就在这个窗口里算），所以从首屏那批里拆出来：
  // 切区间只重取这两张表，KPI、检索方式、高频提问、主题分类都还按近 7 天
  const loadUsage = useCallback(async () => {
    setUsageLoading(true);
    try {
      const [kb, svc] = await Promise.all([listKbUsage(spaceId, window), listServiceUsage(spaceId, window)]);
      setKbUsage(kb);
      setServiceUsage(svc);
    } catch {
      setKbUsage([]);
      setServiceUsage([]);
    } finally {
      setUsageLoading(false);
    }
  }, [spaceId, window]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    void loadUsage();
  }, [loadUsage]);

  const kpiItems: StatCardItem[] = useMemo(
    () =>
      (overview?.kpis ?? []).map((kpi) => ({
        key: kpi.key,
        label: t(kpi.labelKey),
        value: kpi.value,
        accent: kpi.accent,
        iconKey: kpi.iconKey,
      })),
    [overview, t],
  );

  // 请求趋势的窗口是卡片自己的事：切区间只重取趋势与两张使用表，KPI 与检索方式还按近 7 天
  const loadTrend = useCallback(() => getSearchTrend(spaceId, window), [spaceId, window]);
  const { data: trend, loading: trendLoading } = useAsyncData(loadTrend);

  /** 检索方式的条宽与数字都走参与率，不是占总数比例（同一次检索可命中多条链路） */
  const modeItems: DoProportionItem[] = useMemo(
    () => (overview?.modes ?? []).map((mode) => ({ key: mode.key, labelKey: mode.labelKey, count: mode.count })),
    [overview],
  );
  const modeRate = useMemo(
    () => new Map<string, number>((overview?.modes ?? []).map((mode) => [mode.key, mode.participationRate])),
    [overview],
  );

  /**
   * 高频提问的「占比」按 Top10 内部合计算 —— 死数据里没有全量提问总数，
   * 接接口后由服务端直接下发比例，这段随之删掉。
   */
  const hotQuestionTotal = useMemo(() => hotQuestions.reduce((sum, item) => sum + item.count, 0) || 1, [hotQuestions]);

  const kpiLoading = loading && !overview;

  const hotColumns = [
    {
      title: t('dataOperations.column.rank'),
      dataIndex: 'rank',
      key: 'rank',
      width: 70,
      render: (v: number) => <span className={`yx-do-rank${v <= 3 ? ' yx-do-rank--top' : ''}`}>{v}</span>,
    },
    {
      title: t('dataOperations.column.question'),
      dataIndex: 'question',
      key: 'question',
      render: (v: string) => <strong>{v}</strong>,
    },
    {
      title: t('dataOperations.column.topic'),
      dataIndex: 'topicKey',
      key: 'topicKey',
      width: 130,
      render: (v: string) => <Tag variant="filled">{t(v)}</Tag>,
    },
    {
      title: t('dataOperations.column.count'),
      dataIndex: 'count',
      key: 'count',
      width: 110,
      align: 'right' as const,
      render: (v: number) => <span className="yx-do-num">{v.toLocaleString()}</span>,
    },
    {
      title: t('dataOperations.column.percent'),
      dataIndex: 'count',
      key: 'percent',
      width: 100,
      align: 'right' as const,
      render: (v: number) => <span className="yx-do-num">{((v / hotQuestionTotal) * 100).toFixed(1)}%</span>,
    },
  ];

  const kbColumns = [
    {
      title: t('dataOperations.column.kbTitle'),
      dataIndex: 'name',
      key: 'name',
      // 点名称进抽屉 C；后端把「不存在 / 无权限 / 不在本空间」并成同一个 404，所以抽屉只会说
      // 「不存在或无权访问」，不会替后端确认这个库存在
      render: (v: string, row: DoUsageItem) => (
        <a className="yx-do-link" onClick={() => setKbTarget(row.id)}>
          {v}
        </a>
      ),
    },
    {
      title: t('dataOperations.column.type'),
      dataIndex: 'typeKey',
      key: 'typeKey',
      width: 120,
      render: (v?: string) => (v ? t(v) : '—'),
    },
    {
      title: t('dataOperations.column.searchCount'),
      dataIndex: 'count',
      key: 'count',
      width: 120,
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

  const serviceColumns = [
    {
      title: t('dataOperations.column.serviceName'),
      dataIndex: 'name',
      key: 'name',
      // 点名称进抽屉 D。这个接口不 404 —— 服务实体查不到时列表本来就回落显示 id，抽屉跟着回落
      render: (v: string, row: DoUsageItem) => (
        <a className="yx-do-link" onClick={() => setServiceTarget(row.id)}>
          {v}
        </a>
      ),
    },
    {
      title: t('dataOperations.column.searchCount'),
      dataIndex: 'count',
      key: 'count',
      width: 120,
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

  /** 分页器右侧的「共 N 条」 */
  const showTotal = (total: number) => t('dataOperations.common.totalItems', { total });

  if (error) {
    return (
      <Result
        status="error"
        title={t('dataOperations.error.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={() => void loadData()}>
            {t('dataOperations.common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <>
      <StatCards items={kpiItems} loading={kpiLoading} />

      <div className="yx-do-two-col">
        <SectionCard
          title={t('dataOperations.section.searchTrendTitle')}
          extra={<WindowRangePicker value={window} onChange={setWindow} />}
        >
          <TrendAreaChart
            data={trend ?? []}
            loading={trendLoading}
            color="#0ea5e9"
            valueName={t('dataOperations.column.count')}
          />
        </SectionCard>

        <SectionCard
          title={t('dataOperations.section.searchModeTitle')}
          description={t('dataOperations.section.searchModeDesc')}
        >
          <ProportionList
            items={modeItems}
            loading={loading}
            percentOf={(item) => modeRate.get(item.key) ?? 0}
            renderValue={(item) => (
              <>
                {item.count.toLocaleString()}
                <span className="yx-do-rate-label">{t('dataOperations.searchMode.participationRate')}</span>
              </>
            )}
          />
        </SectionCard>
      </div>

      <div className="yx-do-three-col">
        <SectionCard
          title={t('dataOperations.section.hotQuestionsTitle')}
          hint={fetchedAt ? t('dataOperations.section.hotQuestionsHint', { time: fetchedAt }) : undefined}
        >
          <Table
            rowKey="rank"
            size="small"
            columns={hotColumns}
            dataSource={hotQuestions}
            loading={loading}
            pagination={false}
          />
        </SectionCard>

        <SectionCard title={t('dataOperations.section.questionTopicsTitle')}>
          <ProportionList items={topics} loading={loading} />
        </SectionCard>
      </div>

      <div className="yx-do-two-col-even">
        <SectionCard title={t('dataOperations.section.kbUsageTitle')}>
          <Table
            rowKey="id"
            size="small"
            columns={kbColumns}
            dataSource={kbUsage}
            loading={usageLoading}
            pagination={{ pageSize: 5, size: 'small', showTotal }}
          />
        </SectionCard>

        <SectionCard title={t('dataOperations.section.serviceUsageTitle')}>
          <Table
            rowKey="id"
            size="small"
            columns={serviceColumns}
            dataSource={serviceUsage}
            loading={usageLoading}
            pagination={{ pageSize: 5, size: 'small', showTotal }}
          />
        </SectionCard>
      </div>

      <KbDetailDrawer kbId={kbTarget} spaceId={spaceId} window={window} onClose={() => setKbTarget(null)} />
      <ServiceDetailDrawer
        serviceId={serviceTarget}
        spaceId={spaceId}
        window={window}
        onClose={() => setServiceTarget(null)}
      />
    </>
  );
}
