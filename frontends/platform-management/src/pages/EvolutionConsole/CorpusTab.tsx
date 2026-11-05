import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Empty, Input, Popconfirm, Space, Spin, Switch, Table, Tag, Tooltip, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  deleteCorpusCase,
  getEvolutionCorpusCoverage,
  listCorpusCases,
  seedEvolutionCorpus,
  updateCorpusCase,
  type CorpusCase,
  type CorpusSeedResult,
  type EvolutionCorpusCoverage,
  type EvolutionCorpusCoverageItem,
} from '../../api/evolution';

/** 版本新鲜度 → antd Tag 颜色（与服务端口径一致：fresh / drifted / missing）。 */
const FRESHNESS_COLOR: Record<string, string> = {
  fresh: 'success',
  drifted: 'warning',
  missing: 'default',
};

const chainKey = (row: EvolutionCorpusCoverageItem) => `${row.kb_id}:${row.ds_id}`;

/** tier 分布摘要：`single_table=2, multi_hop=1`；空分布显示 `-`。 */
const tierSummary = (byTier: Record<string, number> | undefined) => {
  const entries = Object.entries(byTier ?? {});
  return entries.length ? entries.map(([tier, n]) => `${tier}=${n}`).join(', ') : '-';
};

const shortHash = (hash: string | null) => (hash ? hash.slice(0, 8) : '-');

const shortTime = (value: string | null) =>
  (value ? value.replace('T', ' ').slice(0, 19) : '-');

/** 演化台 Tab5 语料：按 (kb, ds) 链的覆盖率（只读）+ 每行「按 Schema 播种」。
 *
 * 覆盖率链集合只含「已有语料 / 已有版本」的链，新源不会出现在表格里；因此
 * 顶部另给一个「指定源播种」入口（KB / DS 手工输入），否则新源无路可走。
 * 播种只派生 `COUNT(*)` 题，去重且不覆盖冲突；成功后刷新覆盖率，并把最近
 * 一次结果常驻回显（message 会消失，回显不会）。 */
export default function CorpusTab() {
  const { t } = useTranslation();
  const [coverage, setCoverage] = useState<EvolutionCorpusCoverage | null>(null);
  const [cases, setCases] = useState<CorpusCase[]>([]);
  const [caseBusy, setCaseBusy] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** 正在播种的链（`kb:ds`），用于只禁用该行按钮。 */
  const [seeding, setSeeding] = useState<string | null>(null);
  /** 顶部「指定源播种」的输入与在飞状态。 */
  const [seedKb, setSeedKb] = useState('');
  const [seedDs, setSeedDs] = useState('');
  const [seedingManual, setSeedingManual] = useState(false);
  const [lastSeed, setLastSeed] = useState<{
    kb: string;
    ds: string;
    result: CorpusSeedResult;
  } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCoverage(await getEvolutionCorpusCoverage());
      setCases((await listCorpusCases()).cases);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  /** 题目启停 / 删除：写接口只认 case_id；成功后刷新整页（覆盖率与列表同源）。 */
  const toggleCase = useCallback(async (row: CorpusCase, enabled: boolean) => {
    if (!row.id) return;
    setCaseBusy(row.id);
    try {
      await updateCorpusCase(row.id, { enabled });
      await load();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setCaseBusy('');
    }
  }, [load, t]);

  const removeCase = useCallback(async (row: CorpusCase) => {
    if (!row.id) return;
    setCaseBusy(row.id);
    try {
      await deleteCorpusCase(row.id);
      message.success(t('evolutionConsole.corpusCaseDeleted', { name: row.name }));
      await load();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setCaseBusy('');
    }
  }, [load, t]);

  useEffect(() => {
    load();
  }, [load]);

  /** 播种共用收口：调接口 → 常驻回显 → 刷新覆盖率。错误由调用方提示。 */
  const runSeed = useCallback(async (kb: string, ds: string) => {
    const result = await seedEvolutionCorpus({ kb, ds, mode: 'schema' });
    setLastSeed({ kb, ds, result });
    message.success(t('evolutionConsole.corpusSeedDone'));
    await load();
  }, [load, t]);

  const doSeed = useCallback(async (row: EvolutionCorpusCoverageItem) => {
    setSeeding(chainKey(row));
    try {
      await runSeed(row.kb_id, row.ds_id);
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('evolutionConsole.corpusSeedFailed'));
    } finally {
      setSeeding(null);
    }
  }, [runSeed, t]);

  const canSeedManual = Boolean(seedKb.trim() && seedDs.trim());

  const doSeedManual = useCallback(async () => {
    if (!canSeedManual) return;
    setSeedingManual(true);
    try {
      await runSeed(seedKb.trim(), seedDs.trim());
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('evolutionConsole.corpusSeedFailed'));
    } finally {
      setSeedingManual(false);
    }
  }, [canSeedManual, runSeed, seedDs, seedKb, t]);

  if (loading && !coverage) {
    return <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>;
  }
  if (error && !coverage) {
    return (
      <Alert type="error" showIcon message={error}
             action={<Button size="small" onClick={load}>{t('common.retry')}</Button>} />
    );
  }
  if (!coverage) return null;

  const rows = coverage.items;
  const columns: ColumnsType<EvolutionCorpusCoverageItem> = [
    {
      title: t('evolutionConsole.corpusColKb'),
      dataIndex: 'kb_id',
      width: 160,
      ellipsis: true,
      render: (v: string) => <code>{v}</code>,
    },
    {
      title: t('evolutionConsole.corpusColDs'),
      dataIndex: 'ds_id',
      width: 220,
      ellipsis: true,
      render: (v: string) => <code>{v}</code>,
    },
    {
      title: t('evolutionConsole.corpusColCases'),
      key: 'cases',
      width: 130,
      render: (_, row) => `${row.enabled}/${row.total}`,
    },
    {
      title: t('evolutionConsole.corpusColTier'),
      key: 'tier',
      width: 200,
      render: (_, row) => tierSummary(row.by_tier),
    },
    {
      title: t('evolutionConsole.corpusColFreshness'),
      dataIndex: 'freshness',
      width: 110,
      render: (v: string) => (
        <Tag color={FRESHNESS_COLOR[v] ?? 'default'}>
          {t(`evolutionConsole.corpusFreshness.${v}`, { defaultValue: v })}
        </Tag>
      ),
    },
    {
      title: t('evolutionConsole.corpusColVersion'),
      dataIndex: 'active_hash',
      width: 110,
      render: (v: string | null) => (v
        ? <Tooltip title={v}><code>{shortHash(v)}</code></Tooltip>
        : '-'),
    },
    {
      title: t('evolutionConsole.corpusColUpdated'),
      dataIndex: 'updated_at',
      width: 170,
      render: (v: string | null) => shortTime(v),
    },
    {
      title: t('evolutionConsole.corpusColActions'),
      key: 'actions',
      width: 140,
      render: (_, row) => (
        <Popconfirm
          title={t('evolutionConsole.corpusSeedConfirm')}
          okText={t('common.confirm')}
          cancelText={t('common.cancel')}
          onConfirm={() => doSeed(row)}
        >
          <Button size="small" loading={seeding === chainKey(row)}>
            {t('evolutionConsole.corpusSeedAction')}
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
             message={t('evolutionConsole.corpusHint')} />
      {error && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={error}
               action={<Button size="small" onClick={load}>{t('common.retry')}</Button>} />
      )}
      {lastSeed && (
        <Alert
          type={lastSeed.result.dry_run ? 'info' : 'success'}
          showIcon
          style={{ marginBottom: 12 }}
          message={(
            <Space size={8} wrap>
              <span>{t('evolutionConsole.corpusSeedDone')}</span>
              <code>{lastSeed.kb}/{lastSeed.ds}</code>
              <Tag>created={lastSeed.result.created}</Tag>
              <Tag>duplicates={lastSeed.result.duplicates}</Tag>
              <Tag>conflicts={lastSeed.result.conflicts}</Tag>
              <Tag>skipped={lastSeed.result.skipped}</Tag>
            </Space>
          )}
        />
      )}
      <Space style={{ marginBottom: 12 }} wrap>
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>
          {t('common.refresh')}
        </Button>
        <span style={{ color: '#8c8c8c' }}>
          {t('evolutionConsole.corpusTotals', {
            chains: coverage.totals.chains,
            total: coverage.totals.total,
            enabled: coverage.totals.enabled,
          })}
        </span>
      </Space>
      {/* 新源没有语料也没有版本 → 不出现在覆盖率表里，必须能从顶部直接播种。 */}
      <Space style={{ marginBottom: 12 }} wrap>
        <Input
          style={{ width: 200 }}
          value={seedKb}
          onChange={(e) => setSeedKb(e.target.value)}
          placeholder={t('evolutionConsole.corpusSeedKbPlaceholder')}
          aria-label={t('evolutionConsole.corpusSeedKbPlaceholder')}
        />
        <Input
          style={{ width: 220 }}
          value={seedDs}
          onChange={(e) => setSeedDs(e.target.value)}
          placeholder={t('evolutionConsole.corpusSeedDsPlaceholder')}
          aria-label={t('evolutionConsole.corpusSeedDsPlaceholder')}
        />
        <Popconfirm
          title={t('evolutionConsole.corpusSeedManualConfirm')}
          okText={t('common.confirm')}
          cancelText={t('common.cancel')}
          disabled={!canSeedManual}
          onConfirm={doSeedManual}
        >
          <Button loading={seedingManual} disabled={!canSeedManual}>
            {t('evolutionConsole.corpusSeedManualAction')}
          </Button>
        </Popconfirm>
      </Space>
      {rows.length ? (
        <Table<EvolutionCorpusCoverageItem>
          rowKey={chainKey}
          size="small"
          pagination={false}
          columns={columns}
          dataSource={rows}
          scroll={{ x: 1240 }}
        />
      ) : (
        <Empty description={t('evolutionConsole.corpusEmpty')} />
      )}

      <div className="yx-card" style={{ marginTop: 16 }}>
        <Space style={{ marginBottom: 8 }} wrap>
          <h3 style={{ margin: 0 }}>{t('evolutionConsole.corpusCasesTitle')}</h3>
          <span style={{ color: '#8c8c8c' }}>{t('evolutionConsole.corpusCasesHint')}</span>
        </Space>
        {cases.length ? (
          <Table<CorpusCase>
            rowKey={(r) => r.id || r.name}
            size="small"
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            dataSource={cases}
            scroll={{ x: 1100 }}
            columns={[
              { title: t('evolutionConsole.corpusCaseName'), dataIndex: 'name', width: 200,
                ellipsis: true, render: (v: string) => <code>{v}</code> },
              { title: t('evolutionConsole.corpusCaseQuestion'), dataIndex: 'question',
                ellipsis: true },
              { title: t('evolutionConsole.corpusCaseExpect'), dataIndex: 'expect', width: 90 },
              { title: t('evolutionConsole.corpusCaseTier'), dataIndex: 'tier', width: 110 },
              { title: t('evolutionConsole.corpusCaseKb'), dataIndex: 'kb_id', width: 150,
                ellipsis: true, render: (v?: string) => <code>{v || '-'}</code> },
              { title: t('evolutionConsole.corpusCaseEnabled'), key: 'enabled', width: 90,
                render: (_: unknown, row: CorpusCase) => (
                  <Switch
                    size="small"
                    checked={row.enabled !== false}
                    loading={caseBusy === row.id}
                    onChange={(checked) => toggleCase(row, checked)}
                  />
                ) },
              { title: t('evolutionConsole.corpusCaseActions'), key: 'actions', width: 90,
                render: (_: unknown, row: CorpusCase) => (
                  <Popconfirm
                    title={t('evolutionConsole.corpusCaseDeleteConfirm', { name: row.name })}
                    okText={t('common.confirm')}
                    cancelText={t('common.cancel')}
                    onConfirm={() => removeCase(row)}
                  >
                    <Button size="small" danger loading={caseBusy === row.id}>
                      {t('evolutionConsole.corpusCaseDelete')}
                    </Button>
                  </Popconfirm>
                ) },
            ]}
          />
        ) : (
          <Empty description={t('evolutionConsole.corpusCasesEmpty')} />
        )}
      </div>
    </div>
  );
}
