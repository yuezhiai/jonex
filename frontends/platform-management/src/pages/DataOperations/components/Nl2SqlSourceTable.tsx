import { Table } from 'antd';
import type { TableProps } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DoNl2SqlSourceStats } from '../types';
import { fmtCount, fmtDuration, fmtRate } from '../utils';
import { NL2SQL_DASH, SourceText } from './Nl2SqlCells';

/** P95 样本数护栏：少于 20 条时不给具体数值（与页壳 KPI/趋势一致） */
const MIN_SAMPLES = 20;

interface Props {
  sources: DoNl2SqlSourceStats[];
  loading: boolean;
  /** 当前生效的源筛选；`''` = 全部（与筛选条下拉同口径） */
  activeSourceId: string;
  /** 选中某源；点击已生效的行时回传 `''` 取消过滤（与失败码分布一致的开关语义） */
  onSourceChange: (sourceId: string) => void;
}

/**
 * 数据源分布块（设计 §2 / §6.2）：源 / 问答量 / 失败率 / SQL P95 / 慢查询数。
 *
 * 行点击把 `dataSourceId` 设成该源，慢查询与失败复盘两块随之按同一过滤条件重取 ——
 * 与筛选条下拉完全等价；空源行（`__unassigned__`）也一样可点、可筛。
 *
 * 数据来自 `overview.by_source`（页面传入「未按源过滤」的那一份），因此按源过滤后
 * 本表仍保留全部源，用户可以在源之间切换，不会塌成只剩当前一行。
 */
export default function Nl2SqlSourceTable({
  sources,
  loading,
  activeSourceId,
  onSourceChange,
}: Props) {
  const { t } = useTranslation();

  const columns: TableProps<DoNl2SqlSourceStats>['columns'] = [
    {
      title: t('dataOperations.nl2sql.column.source'),
      dataIndex: 'data_source_id',
      key: 'data_source_id',
      width: 220,
      render: (_: string, row) => (
        <SourceText dataSourceId={row.data_source_id} sourceName={row.source_name} />
      ),
    },
    {
      title: t('dataOperations.nl2sql.column.requests'),
      dataIndex: 'requests',
      key: 'requests',
      width: 110,
      align: 'right',
      render: (value: number) => <span className="yx-do-num">{fmtCount(value)}</span>,
    },
    {
      title: t('dataOperations.nl2sql.column.failRate'),
      dataIndex: 'failed',
      key: 'failRate',
      width: 120,
      align: 'right',
      render: (_: number, row) =>
        row.requests === 0 ? (
          <span className="yx-do-num">{NL2SQL_DASH}</span>
        ) : (
          <span className="yx-do-num">{fmtRate((row.failed / row.requests) * 100)}</span>
        ),
    },
    {
      title: t('dataOperations.nl2sql.column.sqlP95'),
      dataIndex: 'sql_p95_ms',
      key: 'sql_p95_ms',
      width: 150,
      align: 'right',
      // 样本不足时显示「—（样本不足）」；后端 NULL 的常规情况 fmtDuration 给「—」
      render: (value: number | null, row) => (
        <span className="yx-do-num">
          {row.sql_samples < MIN_SAMPLES
            ? t('dataOperations.nl2sql.kpi.insufficientSamples')
            : fmtDuration(value)}
        </span>
      ),
    },
    {
      title: t('dataOperations.nl2sql.column.slowCount'),
      dataIndex: 'slow_count',
      key: 'slow_count',
      width: 110,
      align: 'right',
      render: (value: number) => <span className="yx-do-num">{fmtCount(value)}</span>,
    },
  ];

  return (
    <Table<DoNl2SqlSourceStats>
      className="yx-do-nl2sql-source"
      rowKey="data_source_id"
      size="small"
      columns={columns}
      dataSource={sources}
      loading={loading}
      pagination={false}
      locale={{ emptyText: t('dataOperations.empty.noData') }}
      rowClassName={(row) =>
        row.data_source_id === activeSourceId ? 'is-active' : ''
      }
      onRow={(row) => ({
        onClick: () =>
          onSourceChange(
            row.data_source_id === activeSourceId ? '' : row.data_source_id,
          ),
        style: { cursor: 'pointer' },
      })}
    />
  );
}
