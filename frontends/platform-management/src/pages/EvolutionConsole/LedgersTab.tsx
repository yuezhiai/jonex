import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Empty, Spin, Table, Tag, Tooltip } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { getLedgers, type LedgerTable, type Ledgers } from '../../api/evolution';
import { relationEdgeOf } from './skipReasons';


type Row = Record<string, unknown>;

const code = (v: unknown) => (v ? <code>{String(v)}</code> : '-');

const aliasColumns = (t: TFunction): ColumnsType<Row> => [
  { title: <Tooltip title="id">{t('evolutionConsole.ledgerColId')}</Tooltip>, dataIndex: 'id', width: 110, render: code },
  // mapping 与 alias 同落这本账（`entry.kind="mapping"`）：把 concept/alias 两列
  // 分派成 op + target_code + 物理三元组，别让它们渲染成空白。
  { title: <Tooltip title="concept / mapping target">{t('evolutionConsole.ledgerColConcept')}</Tooltip>,
    dataIndex: 'concept', width: 200,
    render: (v: unknown, r: Row) => (isToolSchemaRow(r)
      ? (
        <span>
          <Tag style={{ marginRight: 4 }}>{String(r.op ?? '-')}</Tag>
          <code>{String(r.name ?? '-')}</code>
        </span>
      )
      : isMappingRow(r)
        ? (
          <span>
            <Tag style={{ marginRight: 4 }}>{String(r.op ?? '-')}</Tag>
            <code>{String(r.target_code ?? '-')}</code>
          </span>
        )
        : (v ? String(v) : '-')) },
  { title: <Tooltip title="alias / mapping triple">{t('evolutionConsole.ledgerColAlias')}</Tooltip>,
    dataIndex: 'alias', width: 200,
    render: (v: unknown, r: Row) => {
      if (isToolSchemaRow(r)) {
        const beforeDescription = toolBeforeDescOf(r);
        const description = r.description;
        return beforeDescription
          ? `${String(beforeDescription)} → ${description ? String(description) : '-'}`
          : (description ? String(description) : '-');
      }
      return isMappingRow(r) ? mappingTripleOf(r) : (v ? String(v) : '-');
    } },
  { title: <Tooltip title="kb">{t('evolutionConsole.ledgerColKb')}</Tooltip>, dataIndex: 'kb', width: 120 },
  { title: <Tooltip title="version_before → version_after">{t('evolutionConsole.ledgerColVersion')}</Tooltip>,
    key: 'version', width: 130,
    render: (_, r) => `${String(r.version_before)}→${String(r.version_after)}` },
  { title: <Tooltip title="verified">{t('evolutionConsole.ledgerColVerified')}</Tooltip>, dataIndex: 'verified', width: 90,
    render: (v: unknown) => (v === true ? <Tag color="success">✓</Tag> : v === false ? <Tag color="error">✗</Tag> : '-') },
  { title: <Tooltip title="backup">{t('evolutionConsole.ledgerColBackup')}</Tooltip>, dataIndex: 'backup', ellipsis: true, render: code },
  { title: <Tooltip title="at">{t('evolutionConsole.ledgerColAt')}</Tooltip>, dataIndex: 'at', width: 170,
    render: (v: unknown) => (v ? String(v).replace('T', ' ').slice(0, 19) : '-') },
  { title: <Tooltip title="note">{t('evolutionConsole.ledgerColNote')}</Tooltip>, dataIndex: 'note', ellipsis: true },
];

const isRelationRow = (r: Row) => r.kind === 'relation';
/** constraint 台账行（P1-4 R4：写在 query_rule 那本账，`entry.kind="constraint"`）：
 * 没有 target_code，只有 `{op, name, expression?, before?}` → 展示 op + name。 */
const isConstraintRow = (r: Row) => r.kind === 'constraint';
/** mapping 台账行（P1-5：写在 alias 那本账，`entry.kind="mapping"`）：
 * 身份是 `target_code` + 物理三元组，槽在 `before` / `after` 里。 */
const isMappingRow = (r: Row) => r.kind === 'mapping';
/** tool_schema 台账行（写 alias 那本账，`entry.kind="tool_schema"`）：
 * 身份是工具 name，改的是 description；回滚行的 kind 同为 tool_schema + op=rollback。 */
const isToolSchemaRow = (r: Row) => r.kind === 'tool_schema';
const toolBeforeDescOf = (r: Row) => {
  const before = r.before;
  if (before && typeof before === 'object' && 'description' in before) {
    return (before as { description?: unknown }).description;
  }
  return undefined;
};
const MAPPING_SLOTS = ['role', 'filter', 'aggregation', 'grain'] as const;
const mappingTripleOf = (r: Row) => [
  String(r.data_source_id ?? ''),
  [r.entity_name, r.attribute_name].filter(Boolean).join('.'),
].filter(Boolean).join(' · ');
const mappingSlotsOf = (r: Row) => {
  const after = (r.after ?? r) as Record<string, unknown>;
  return MAPPING_SLOTS
    .filter((slot) => after[slot] != null && after[slot] !== '')
    .map((slot) => `${slot}=${String(after[slot])}`)
    .join(', ');
};
const beforeExpressionOf = (r: Row) => {
  const before = r.before;
  if (before && typeof before === 'object' && 'expression' in before) {
    return (before as { expression?: unknown }).expression;
  }
  return undefined;
};

/** relation / constraint 台账行（都写 query_rule 那本账）没有完整 target_code，
 * 这两列按 kind 分派，别让它们渲染成空白。 */
const queryRuleColumns = (t: TFunction): ColumnsType<Row> => [
  { title: <Tooltip title="target_code / relation edge / constraint op+name">{t('evolutionConsole.ledgerColTarget')}</Tooltip>,
    key: 'target', width: 240, ellipsis: true,
    render: (_, r) => {
      if (isRelationRow(r)) {
        // P1 收尾：新台账行带 op（add/remove）；旧行没有该键 → 只显示两端，形状不变。
        const op = typeof r.op === 'string' && r.op ? r.op : null;
        const edge = code(relationEdgeOf(r) ?? r.name);
        if (!op) return edge;
        return (
          <span>
            <Tag style={{ marginRight: 4 }}>
              {t(`evolutionConsole.relationOp.${op}`, { defaultValue: op })}
            </Tag>
            {edge}
          </span>
        );
      }
      if (isConstraintRow(r)) {
        return (
          <span>
            <Tag style={{ marginRight: 4 }}>{String(r.op ?? '-')}</Tag>
            <code>{String(r.name ?? '-')}</code>
          </span>
        );
      }
      if (isMappingRow(r)) {
        return (
          <span>
            <Tag style={{ marginRight: 4 }}>{String(r.op ?? '-')}</Tag>
            <code>{String(r.target_code ?? '-')}</code>
            <span style={{ marginLeft: 4, color: '#8c8c8c', fontSize: 12 }}>
              {mappingTripleOf(r)}
            </span>
          </span>
        );
      }
      return code(r.target_code);
    } },
  { title: <Tooltip title="expression / relation description / mapping slots">{t('evolutionConsole.ledgerColExpression')}</Tooltip>,
    key: 'expression', ellipsis: true,
    render: (_, r) => {
      if (isRelationRow(r)) return String(r.description ?? r.cardinality ?? '');
      if (isConstraintRow(r)) {
        // update 行有 r.expression；disable/enable 只有 before.enabled，表达式列留空。
        return String(r.expression ?? beforeExpressionOf(r) ?? '');
      }
      if (isMappingRow(r)) return mappingSlotsOf(r);
      return String(r.expression ?? '');
    } },
  { title: <Tooltip title="kb">{t('evolutionConsole.ledgerColKb')}</Tooltip>, dataIndex: 'kb', width: 120 },
  { title: <Tooltip title="ds_id">{t('evolutionConsole.ledgerColDs')}</Tooltip>, dataIndex: 'ds_id', width: 260, ellipsis: true, render: code },
  { title: <Tooltip title="version_before → version_after">{t('evolutionConsole.ledgerColVersion')}</Tooltip>,
    key: 'version', width: 130,
    render: (_, r) => `${String(r.version_before)}→${String(r.version_after)}` },
  { title: <Tooltip title="verified">{t('evolutionConsole.ledgerColVerified')}</Tooltip>, dataIndex: 'verified', width: 90,
    render: (v: unknown) => (v === true ? <Tag color="success">✓</Tag> : v === false ? <Tag color="error">✗</Tag> : '-') },
  { title: <Tooltip title="from_file">{t('evolutionConsole.ledgerColFromFile')}</Tooltip>, dataIndex: 'from_file', ellipsis: true, render: code },
  { title: <Tooltip title="backup">{t('evolutionConsole.ledgerColBackup')}</Tooltip>, dataIndex: 'backup', ellipsis: true, render: code },
  { title: <Tooltip title="at">{t('evolutionConsole.ledgerColAt')}</Tooltip>, dataIndex: 'at', width: 170,
    render: (v: unknown) => (v ? String(v).replace('T', ' ').slice(0, 19) : '-') },
];

const rejectColumns = (t: TFunction): ColumnsType<Row> => [
  { title: <Tooltip title="id">{t('evolutionConsole.ledgerColId')}</Tooltip>, dataIndex: 'id', width: 160, render: code },
  { title: <Tooltip title="at">{t('evolutionConsole.ledgerColAt')}</Tooltip>, dataIndex: 'at', width: 170,
    render: (v: unknown) => (v ? String(v).replace('T', ' ').slice(0, 19) : '-') },
  { title: <Tooltip title="note">{t('evolutionConsole.ledgerColNote')}</Tooltip>, dataIndex: 'note', ellipsis: true },
];

function LedgerBlock({ title, table, columns, x }: {
  title: string;
  table: LedgerTable | undefined;
  columns: ColumnsType<Row>;
  /** 表级横向滚动下限 = 固定列宽总和 + 弹性列预留（每列 200）。窄容器下列头不再被压塌。 */
  x: number;
}) {
  const { t } = useTranslation();
  if (!table) return null;
  return (
    <div style={{ marginBottom: 24 }}>
      <h4 style={{ margin: '8px 0 4px' }}>
        {title}
        <span style={{ marginLeft: 8, color: '#8c8c8c', fontSize: 12, fontWeight: 400 }}>
          <code>{table.path}</code>
          {table.mtime ? `\u3000${t('evolutionConsole.ledgerMtime')}: ${table.mtime.replace('T', ' ').slice(0, 19)}` : ''}
          {table.exists && !table.unreadable ? `\u3000${t('evolutionConsole.ledgerRows', { n: table.items.length })}` : ''}
        </span>
      </h4>
      {/* 台账按事件追加：同一提案可有多行（id 重复），rowKey 须组合字段且不用已弃用的 index 参数。 */}
      {table.exists ? (
        table.unreadable ? (
          <Alert type="error" showIcon message={t('evolutionConsole.ledgerUnreadable')} />
        ) : (
          <Table<Row> rowKey={(r) => [
                          r.id, r.concept, r.alias, r.target_code, r.name,
                          r.data_source_id, r.entity_name, r.attribute_name, r.at,
                        ].filter(Boolean).join(':')}
                      size="small" pagination={false} columns={columns} dataSource={table.items}
                      scroll={{ x }} />
        )
      ) : (
        <Empty description={t('evolutionConsole.ledgerMissing')} />
      )}
    </div>
  );
}

/** Tab3 台账：三本服务端台账（alias 采纳 / query_rule 采纳 / 提案否决）。 */
export default function LedgersTab() {
  const { t } = useTranslation();
  const [ledgers, setLedgers] = useState<Ledgers | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setLedgers(await getLedgers());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !ledgers) {
    return <div style={{ textAlign: 'center', padding: 32 }}><Spin /></div>;
  }
  if (error && !ledgers) {
    return (
      <Alert type="error" showIcon message={error}
             action={<Button size="small" onClick={load}>{t('common.retry')}</Button>} />
    );
  }
  if (!ledgers) return null;

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('evolutionConsole.ledgersHint')} />
      {error && ledgers && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={error}
               action={<Button size="small" onClick={load}>{t('common.retry')}</Button>} />
      )}
      <Button size="small" icon={<ReloadOutlined />} onClick={load} loading={loading} style={{ marginBottom: 12 }}>
        {t('common.refresh')}
      </Button>
      <LedgerBlock title={t('evolutionConsole.ledgerAlias')} table={ledgers.alias}
                   columns={aliasColumns(t)} x={1280} />
      <LedgerBlock title={t('evolutionConsole.ledgerQueryRule')} table={ledgers.query_rule}
                   columns={queryRuleColumns(t)} x={1540} />
      <LedgerBlock title={t('evolutionConsole.ledgerRejects')} table={ledgers.rejects}
                   columns={rejectColumns(t)} x={530} />
    </div>
  );
}
