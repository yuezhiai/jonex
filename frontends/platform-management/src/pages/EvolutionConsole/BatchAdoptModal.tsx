import React, { useEffect, useState } from 'react';
import { Alert, Input, Modal, Space, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import type {
  BatchAdoptResult,
  BatchAdoptResultItem,
  EvolutionRunSummary,
} from '../../api/evolution';

/** 一次批量采纳的 run 上限（与服务端 `MAX_BATCH_RUNS` 同口径）。 */
export const MAX_BATCH_RUNS = 10;

export type ChainScope = 'abstract' | 'synonym' | 'compiled';

/** kind → 版本链 scope：与后端 `locking._KIND_TO_SCOPE` 逐字对齐。
 * `alias` / `mapping` / `tool_schema` 共用 abstract（KB 级）链；`synonym`
 * （值同义词）是**独立**的 KB 级链（不与 abstract 互斥）；`query_rule` /
 * `relation` / `constraint` 共用 compiled（按 ds）链。 */
const SCOPE_BY_KIND: Record<string, ChainScope> = {
  alias: 'abstract',
  mapping: 'abstract',
  tool_schema: 'abstract',
  synonym: 'synonym',
  query_rule: 'compiled',
  relation: 'compiled',
  constraint: 'compiled',
};

/** 未知/缺失 kind → null（无法推导链键：既不能判重，也不能交给服务端）。 */
export function chainScopeOf(kind: string | null | undefined): ChainScope | null {
  if (!kind) return null;
  return SCOPE_BY_KIND[kind] ?? null;
}

/** 链键 `(kb, scope, ds)` 的前端字符串形式（与服务端 `validate_batch` 同口径）。
 * abstract / synonym 链不带 ds；compiled 链带 ds（缺 ds 归一为空串，仍然可判重）。 */
export function chainKeyOf(
  run: Pick<EvolutionRunSummary, 'kind' | 'kb' | 'ds'>,
): string | null {
  const scope = chainScopeOf(run.kind);
  if (!scope || !run.kb) return null;
  const ds = scope === 'compiled' ? (run.ds ?? '') : '';
  return `${run.kb}|${scope}|${ds}`;
}

interface BatchAdoptModalProps {
  open: boolean;
  /** 打开弹窗时选中的 run（快照：提交后列表刷新不影响弹窗展示）。 */
  items: EvolutionRunSummary[];
  submitting: boolean;
  result: BatchAdoptResult | null;
  error: string | null;
  onCancel: () => void;
  onSubmit: (overrideReason: string | undefined) => void;
}

/** 批量采纳确认弹窗（P1）：列 run / kb / 链 + 可选 override_reason。
 * **批量即确认**：没有预览、没有 confirm 参数，提交即受理；结果逐条展示。 */
export default function BatchAdoptModal({
  open,
  items,
  submitting,
  result,
  error,
  onCancel,
  onSubmit,
}: BatchAdoptModalProps) {
  const { t } = useTranslation();
  const [overrideReason, setOverrideReason] = useState('');

  useEffect(() => {
    if (open) setOverrideReason('');
  }, [open]);

  const chainText = (run: EvolutionRunSummary) => {
    const scope = chainScopeOf(run.kind);
    if (!scope) return '-';
    const base = t(`evolutionConsole.batchAdoptChainScope.${scope}`);
    return scope === 'compiled' ? `${base} · ${run.ds ?? '-'}` : base;
  };

  const itemColumns: ColumnsType<EvolutionRunSummary> = [
    {
      title: t('evolutionConsole.batchAdoptColRun'),
      dataIndex: 'run_id',
      width: 180,
      ellipsis: true,
      render: (v: string) => <code>{v}</code>,
    },
    {
      title: t('evolutionConsole.batchAdoptColKb'),
      dataIndex: 'kb',
      width: 180,
      ellipsis: true,
      render: (v: string | null) => v ?? '-',
    },
    {
      title: t('evolutionConsole.batchAdoptColKind'),
      dataIndex: 'kind',
      width: 130,
      render: (v: string | null) => (v
        ? t(`evolutionConsole.kind.${v}`, { defaultValue: v })
        : '-'),
    },
    {
      title: t('evolutionConsole.batchAdoptColChain'),
      key: 'chain',
      ellipsis: true,
      render: (_: unknown, row) => chainText(row),
    },
  ];

  const resultColumns: ColumnsType<BatchAdoptResultItem> = [
    {
      title: t('evolutionConsole.batchAdoptColRun'),
      dataIndex: 'run_id',
      width: 180,
      ellipsis: true,
      render: (v: string) => <code>{v}</code>,
    },
    {
      title: t('evolutionConsole.batchAdoptColOutcome'),
      dataIndex: 'accepted',
      width: 120,
      render: (accepted: boolean) => (accepted ? (
        <Tag color="success">{t('evolutionConsole.batchAdoptAccepted')}</Tag>
      ) : (
        <Tag color="error">{t('evolutionConsole.batchAdoptRejected')}</Tag>
      )),
    },
    {
      title: t('evolutionConsole.batchAdoptColStatus'),
      dataIndex: 'status',
      width: 120,
      render: (v: string | null) => v ?? '-',
    },
    {
      title: t('evolutionConsole.batchAdoptColMessage'),
      dataIndex: 'message',
      ellipsis: true,
      render: (_: unknown, row) => {
        if (row.accepted) return row.idempotent_replay ? t('evolutionConsole.batchAdoptReplay') : '-';
        const prefix = row.code == null ? '' : `[${row.code}] `;
        return `${prefix}${row.message ?? '-'}`;
      },
    },
  ];

  return (
    <Modal
      open={open}
      title={t('evolutionConsole.batchAdoptTitle', { n: items.length })}
      onCancel={onCancel}
      onOk={() => onSubmit(overrideReason.trim() || undefined)}
      confirmLoading={submitting}
      okText={t('evolutionConsole.batchAdoptSubmit')}
      cancelText={t('common.cancel')}
      // 结果回来后禁用确认：避免对着同一批重复提交（受理是幂等的，但会误导）。
      okButtonProps={{ danger: true, disabled: submitting || Boolean(result) || !items.length }}
      width={760}
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message={t('evolutionConsole.batchAdoptWarn')}
      />

      <Table<EvolutionRunSummary>
        rowKey="run_id"
        size="small"
        pagination={false}
        columns={itemColumns}
        dataSource={items}
        style={{ marginBottom: 12 }}
      />

      <Input.TextArea
        rows={2}
        value={overrideReason}
        placeholder={t('evolutionConsole.batchAdoptOverridePlaceholder')}
        onChange={(e) => setOverrideReason(e.target.value)}
      />

      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message={t('evolutionConsole.batchAdoptFailed')}
          description={error}
        />
      )}

      {result && (
        <div style={{ marginTop: 12 }}>
          <Alert
            type={result.rejected > 0 ? 'warning' : 'success'}
            showIcon
            style={{ marginBottom: 8 }}
            message={t('evolutionConsole.batchAdoptSummary', {
              accepted: result.accepted,
              rejected: result.rejected,
            })}
            description={(
              <Space size={8} wrap>
                <span>{t('evolutionConsole.batchAdoptBatchId')}: <code>{result.batch_id}</code></span>
                <span>{t('evolutionConsole.batchAdoptBackground')}</span>
              </Space>
            )}
          />
          <Table<BatchAdoptResultItem>
            rowKey="run_id"
            size="small"
            pagination={false}
            columns={resultColumns}
            dataSource={result.results}
          />
        </div>
      )}
    </Modal>
  );
}
