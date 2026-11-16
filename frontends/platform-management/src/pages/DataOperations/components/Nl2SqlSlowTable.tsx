import { Table } from 'antd';
import type { TableProps } from 'antd';
import { formatDateTime } from '@jonex/shared-lib';
import { useTranslation } from 'react-i18next';
import type { DoNl2SqlRow } from '../types';
import { fmtDuration, truncateText } from '../utils';
import { A1bTag, NL2SQL_DASH, OutcomeTag, SourceText } from './Nl2SqlCells';

interface Props {
  rows: DoNl2SqlRow[];
  total: number;
  loading: boolean;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onOpen: (row: DoNl2SqlRow) => void;
}

/**
 * 慢查询表（设计 §6.2）。
 *
 * 分页与排序都在服务端：默认按 SQL 执行耗时倒序（`sort='sql'`），
 * 组件自身不排序、不切片 —— 否则「当前页第 1 条」就不再是后端口径下的最慢一条。
 * 行点击开现场抽屉，页面本身只读。
 */
export default function Nl2SqlSlowTable({ rows, total, loading, page, pageSize, onPageChange, onOpen }: Props) {
  const { t } = useTranslation();

  const columns: TableProps<DoNl2SqlRow>['columns'] = [
    {
      title: t('dataOperations.nl2sql.column.time'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 160,
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: t('dataOperations.nl2sql.column.source'),
      dataIndex: 'data_source_id',
      key: 'data_source_id',
      width: 160,
      render: (_: string | null, row) => (
        <SourceText dataSourceId={row.data_source_id} sourceName={row.source_name} />
      ),
    },
    {
      title: t('dataOperations.nl2sql.column.question'),
      dataIndex: 'question',
      key: 'question',
      render: (value: string | null) => (
        <span title={value ?? undefined}>{value ? truncateText(value, 60) : NL2SQL_DASH}</span>
      ),
    },
    {
      title: t('dataOperations.nl2sql.column.sqlDuration'),
      dataIndex: 'elapsed_ms',
      key: 'elapsed_ms',
      width: 120,
      align: 'right',
      render: (value: number | null) => <span className="yx-do-num">{fmtDuration(value)}</span>,
    },
    {
      title: t('dataOperations.nl2sql.column.totalDuration'),
      dataIndex: 'total_ms',
      key: 'total_ms',
      width: 130,
      align: 'right',
      render: (value: number | null) => <span className="yx-do-num">{fmtDuration(value)}</span>,
    },
    {
      title: t('dataOperations.nl2sql.column.rowCount'),
      dataIndex: 'row_count',
      key: 'row_count',
      width: 100,
      align: 'right',
      render: (value: number | null) =>
        value == null ? NL2SQL_DASH : <span className="yx-do-num">{value.toLocaleString()}</span>,
    },
    {
      title: t('dataOperations.nl2sql.column.outcome'),
      dataIndex: 'outcome',
      key: 'outcome',
      width: 100,
      render: (value: string | null) => <OutcomeTag outcome={value} />,
    },
    {
      title: t('dataOperations.nl2sql.column.a1b'),
      dataIndex: 'precompile_retried',
      key: 'precompile_retried',
      width: 100,
      render: (value: boolean) => <A1bTag retried={value} />,
    },
  ];

  return (
    <Table<DoNl2SqlRow>
      rowKey="id"
      size="small"
      columns={columns}
      dataSource={rows}
      loading={loading}
      locale={{ emptyText: t('dataOperations.nl2sql.empty.noSlowQueries') }}
      onRow={(row) => ({
        onClick: () => onOpen(row),
        // 行可点开抽屉：光标给出唯一的可交互提示，不额外加一个「查看」列
        style: { cursor: 'pointer' },
      })}
      pagination={{
        current: page,
        pageSize,
        total,
        size: 'small',
        showSizeChanger: false,
        showTotal: (count) => t('dataOperations.common.totalItems', { total: count }),
        onChange: onPageChange,
      }}
    />
  );
}
