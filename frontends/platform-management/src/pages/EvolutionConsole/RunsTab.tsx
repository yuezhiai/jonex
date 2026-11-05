import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Empty, Popconfirm, Space, Table, Tag, Tooltip, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import {
  batchAdoptRuns,
  listEvolutionRuns,
  rollbackRun,
  type BatchAdoptBody,
  type BatchAdoptResult,
  type EvolutionStatus,
  type EvolutionRunSummary,
} from '../../api/evolution';
import BatchAdoptModal, { MAX_BATCH_RUNS, chainKeyOf } from './BatchAdoptModal';
import { isRollbackDone, parseStage, stageLabelKey, statusTagColor } from './constants';
import { benchStatusColor } from './components/BenchCard';
import StartRunModal from './StartRunModal';

interface RunsTabProps {
  status: EvolutionStatus | null;
  /** 让容器刷新 GET /status（发起成功/失败后调用）。 */
  onChanged: () => void;
}

const POLL_MS = 10_000;

/** 与服务端 `has_active` 同口径（run_store.py）：只有这两种状态算「运行中」。 */
const RUNNING_STATUSES = new Set(['queued', 'running']);

/** Tab2 演化运行：run 列表（活跃时 10s 轮询）+ 发起 run + stale/stuck 标注与回滚。 */
export default function RunsTab({ status, onChanged }: RunsTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [runs, setRuns] = useState<EvolutionRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [rollingBack, setRollingBack] = useState<string | null>(null);
  // 批量采纳（P1）：选择是「同 KB + 每链一个 run」的受限多选。
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchItems, setBatchItems] = useState<EvolutionRunSummary[]>([]);
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const [batchResult, setBatchResult] = useState<BatchAdoptResult | null>(null);
  const [batchError, setBatchError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await listEvolutionRuns();
      setRuns(res.runs);
      // 刷新后剔除已不在列表里的选择（保持引用不变，避免无谓重渲染）。
      setSelectedRunIds((prev) => {
        const alive = prev.filter((id) => res.runs.some((r) => r.run_id === id));
        return alive.length === prev.length ? prev : alive;
      });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  // 服务端 `has_active` 只认 queued|running（run_store.py）：adopting 不阻塞发起，
  // 但仍要轮询推进采纳态。两者拆开，别让「忙」与「不可发起」混成一个开关。
  //
  // `status.active_run` 只在 mount / `onChanged` 刷新，running → adopting 后会残留；
  // 因此用轮询列表对账：列表里同 id 已不是 queued/running → 不再当成运行中。
  // 列表里没有该 id（可能被分页/截断）→ 保持旧判定，不能当作已结束。
  const activeRunId = status?.active_run?.run_id ?? null;
  const activeRunRow = activeRunId
    ? runs.find((r) => r.run_id === activeRunId)
    : undefined;
  const activeRunRunning = activeRunId !== null
    && (!activeRunRow || RUNNING_STATUSES.has(activeRunRow.status));
  const isRunning = activeRunRunning
    || runs.some((r) => RUNNING_STATUSES.has(r.status) && !r.stale);
  const hasBusy = isRunning || runs.some((r) => r.status === 'adopting' && !r.stale);

  useEffect(() => {
    if (!hasBusy) return;
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [hasBusy, load]);

  const stageText = (stage: string) => {
    const { base, failed } = parseStage(stage);
    if (!base) return '-';
    const label = t(stageLabelKey(base), { defaultValue: base });
    return failed ? t('evolutionConsole.stageFailed', { stage: label }) : label;
  };

  const doRollback = async (runId: string) => {
    setRollingBack(runId);
    try {
      const res = await rollbackRun(runId);
      if (res.ok) {
        if (isRollbackDone(res.stdout_tail)) {
          message.success(t('evolutionConsole.rollbackDone'));
        } else {
          message.info(t('evolutionConsole.rollbackNoop'));
        }
      } else {
        message.error(
          t('evolutionConsole.rollbackFailed', {
            rc: res.rc,
            tail: (res.stderr_tail || res.stdout_tail || '-').slice(-200),
          }),
        );
      }
      await load();
      onChanged();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setRollingBack(null);
    }
  };

  // ── 批量采纳：选择约束（同 KB + 链键 `(kb, scope(kind), ds)` 唯一）──
  const selectedSet = new Set(selectedRunIds);
  const selectedRows = runs.filter((row) => selectedSet.has(row.run_id));
  const selectedKb = selectedRows[0]?.kb ?? null;
  const selectedChains = new Map<string, string>();
  selectedRows.forEach((row) => {
    const chain = chainKeyOf(row);
    if (chain) selectedChains.set(chain, row.run_id);
  });

  /** 勾选框禁用与原因：已选行本身永远可点（否则无法取消选择）。 */
  const checkboxPropsFor = (row: EvolutionRunSummary) => {
    if (selectedSet.has(row.run_id)) return {};
    const chain = chainKeyOf(row);
    if (!chain) {
      return { disabled: true, title: t('evolutionConsole.batchSelectUnavailable') };
    }
    if (selectedKb && row.kb !== selectedKb) {
      return {
        disabled: true,
        title: t('evolutionConsole.batchSelectOtherKb', { kb: selectedKb }),
      };
    }
    const clash = selectedChains.get(chain);
    if (clash) {
      return {
        disabled: true,
        title: t('evolutionConsole.batchSelectSameChain', { runId: clash }),
      };
    }
    if (selectedRows.length >= MAX_BATCH_RUNS) {
      return { disabled: true, title: t('evolutionConsole.batchSelectLimit', { n: MAX_BATCH_RUNS }) };
    }
    return {};
  };

  /** 收敛选择：表头全选会一次返回所有「当前未禁用」的行，必须再按约束贪心过滤，
   * 否则仍可能选到同链/跨 KB 的两条（禁用态是渲染快照，挡不住同一次批量事件）。 */
  const applySelection = (keys: React.Key[]) => {
    const wanted = new Set(keys.map(String));
    const kept: string[] = [];
    const dropped: string[] = [];
    let kb: string | null = null;
    const chains = new Set<string>();
    runs.forEach((row) => {
      if (!wanted.has(row.run_id)) return;
      const chain = chainKeyOf(row);
      if (!chain) {
        dropped.push(row.run_id);
        return;
      }
      if (kb === null) kb = row.kb;
      if (row.kb !== kb || chains.has(chain) || kept.length >= MAX_BATCH_RUNS) {
        dropped.push(row.run_id);
        return;
      }
      chains.add(chain);
      kept.push(row.run_id);
    });
    setSelectedRunIds(kept);
    if (dropped.length) {
      message.warning(t('evolutionConsole.batchSelectDropped', { runIds: dropped.join(', ') }));
    }
  };

  const openBatchAdopt = () => {
    setBatchItems(selectedRows);
    setBatchResult(null);
    setBatchError(null);
    setBatchOpen(true);
  };

  const submitBatchAdopt = async (overrideReason?: string) => {
    if (!batchItems.length) return;
    setBatchSubmitting(true);
    setBatchError(null);
    try {
      const body: BatchAdoptBody = { run_ids: batchItems.map((row) => row.run_id) };
      if (overrideReason) body.override_reason = overrideReason;
      const res = await batchAdoptRuns(body);
      setBatchResult(res);
      if (res.rejected > 0) {
        message.warning(t('evolutionConsole.batchAdoptSummary', {
          accepted: res.accepted,
          rejected: res.rejected,
        }));
      } else {
        message.success(t('evolutionConsole.batchAdoptDone'));
      }
      setSelectedRunIds([]);
      await load();
      onChanged();
    } catch (e: unknown) {
      setBatchError(e instanceof Error ? e.message : t('common.error'));
    } finally {
      setBatchSubmitting(false);
    }
  };

  const columns: ColumnsType<EvolutionRunSummary> = [
    {
      title: t('evolutionConsole.runId'),
      dataIndex: 'run_id',
      // run_id 形如 run_4660d3988dc4（16 字符）：90px 会被挤进「类型」列；
      // 加宽到 170 并 ellipsis，完整值放 tooltip，避免挡住 kind。
      width: 170,
      ellipsis: true,
      render: (v: string, row) => (
        <Tooltip title={v}>
          <Space size={4} style={{ maxWidth: '100%' }}>
            <a style={{ display: 'inline-block', maxWidth: 135, overflow: 'hidden',
                        textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        verticalAlign: 'bottom' }}
               onClick={() => navigate(`/evolution/${encodeURIComponent(v)}`)}>{v}</a>
            {row.stale && <Tag color="warning">{t('evolutionConsole.staleTag')}</Tag>}
            {row.stuck && (
              <Tooltip title={t('evolutionConsole.stuckTag')}>
                <Tag color="error">{t('evolutionConsole.stuckTag')}</Tag>
              </Tooltip>
            )}
          </Space>
        </Tooltip>
      ),
    },
    {
      title: t('evolutionConsole.runKind'),
      dataIndex: 'kind',
      width: 110,
      render: (v: string | null) =>
        v ? (
          <Tooltip title={v}>
            <span>{t(`evolutionConsole.kind.${v}`, { defaultValue: v })}</span>
          </Tooltip>
        ) : (
          '-'
        ),
    },
    { title: t('evolutionConsole.runStage'), dataIndex: 'stage', width: 140, render: stageText },
    {
      title: t('evolutionConsole.runStatus'),
      dataIndex: 'status',
      width: 110,
      render: (v: string) => <Tag color={statusTagColor(v)}>{t(`evolutionConsole.status.${v}`, { defaultValue: v })}</Tag>,
    },
    {
      title: t('evolutionConsole.runGate'),
      dataIndex: 'gate',
      width: 180,
      render: (gate: EvolutionRunSummary['gate']) =>
        gate ? (
          <Tooltip title={t('evolutionConsole.gateTooltip', { decision: gate.decision, delta: gate.delta })}>
            <Space size={4}>
              <Tag color={gate.decision === 'accept' ? 'success' : gate.decision === 'uncertain' ? 'warning' : 'error'}>
                {t(`evolutionConsole.decision.${gate.decision}`, { defaultValue: gate.decision })}
              </Tag>
              {Array.isArray(gate.passes) && gate.passes.length === 2 && (
                <span>
                  {gate.passes[0]}→{gate.passes[1]}
                </span>
              )}
            </Space>
          </Tooltip>
        ) : '-',
    },
    {
      title: t('evolutionConsole.runBench'),
      dataIndex: 'bench_status',
      width: 110,
      render: (v: string | null | undefined) => (v ? (
        <Tag color={benchStatusColor(v)}>
          {t(`evolutionConsole.benchStatus.${v}`, { defaultValue: v })}
        </Tag>
      ) : (
        <Tag>{t('evolutionConsole.benchNotRun')}</Tag>
      )),
    },
    { title: t('evolutionConsole.runEntries'), dataIndex: 'entries_count', width: 80,
      render: (v: number | null) => v ?? '-' },
    {
      title: t('evolutionConsole.runUpdated'),
      dataIndex: 'updated_at',
      width: 170,
      render: (v: string | null) => (v ? v.replace('T', ' ').slice(0, 19) : '-'),
    },
    {
      title: t('evolutionConsole.runError'),
      dataIndex: 'error',
      ellipsis: true,
      render: (v: string) => v || '-',
    },
    {
      title: '',
      key: 'actions',
      width: 160,
      render: (_: unknown, row) => (
        <Space size={4}>
          <Button size="small" type="link"
                  onClick={() => navigate(`/evolution/${encodeURIComponent(row.run_id)}`)}>
            {t('evolutionConsole.viewDetail')}
          </Button>
          {row.stuck && (
            <Popconfirm title={t('evolutionConsole.rollbackConfirm')}
                        onConfirm={() => doRollback(row.run_id)}>
              <Button size="small" danger loading={rollingBack === row.run_id}>
                {t('evolutionConsole.rollbackNow')}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Tooltip title={isRunning ? t('evolutionConsole.startRunBlocked') : ''}>
          <Button type="primary" disabled={isRunning} onClick={() => setModalOpen(true)}>
            {t('evolutionConsole.startRun')}
          </Button>
        </Tooltip>
        <Tooltip title={selectedRunIds.length ? '' : t('evolutionConsole.batchAdoptEmpty')}>
          <Button disabled={!selectedRunIds.length} onClick={openBatchAdopt}>
            {t('evolutionConsole.batchAdopt')}
          </Button>
        </Tooltip>
        <Button icon={<ReloadOutlined />} onClick={load}>
          {t('common.refresh')}
        </Button>
        {hasBusy && <span style={{ color: '#8c8c8c' }}>{t('evolutionConsole.pollHint')}</span>}
      </Space>

      {error && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={error}
               action={<Button size="small" onClick={load}>{t('common.retry')}</Button>} />
      )}

      <Table<EvolutionRunSummary>
        rowKey="run_id"
        size="small"
        loading={loading}
        columns={columns}
        rowSelection={{
          selectedRowKeys: selectedRunIds,
          onChange: (keys) => applySelection(keys),
          getCheckboxProps: checkboxPropsFor,
        }}
        dataSource={runs}
        scroll={{ x: 1200 }}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        locale={{ emptyText: <Empty description={t('evolutionConsole.noRuns')} /> }}
      />

      <StartRunModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onStarted={(runId) => {
          setModalOpen(false);
          onChanged();
          navigate(`/evolution/${encodeURIComponent(runId)}`);
        }}
        onFailed={onChanged}
      />

      <BatchAdoptModal
        open={batchOpen}
        items={batchItems}
        submitting={batchSubmitting}
        result={batchResult}
        error={batchError}
        onCancel={() => setBatchOpen(false)}
        onSubmit={submitBatchAdopt}
      />
    </div>
  );
}
