import { Table } from 'antd';
import type { TableProps } from 'antd';
import { formatDateTime } from '@jonex/shared-lib';
import { useTranslation } from 'react-i18next';
import type { DoNl2SqlReasonCode, DoNl2SqlRow, DoProportionItem } from '../types';
import { truncateText } from '../utils';
import { A1bTag, NL2SQL_DASH, ReasonCodeText, SourceText } from './Nl2SqlCells';
import ProportionList from './ProportionList';

/**
 * 空失败码的对外哨兵。
 *
 * 后端 `group by reason_code` 的空桶是 `null`（前端显示「未分类」），而列表过滤
 * 只认这个字符串。后端会把 `__unclassified__` 翻译成 `reason_code IS NULL`，
 * 因此这里**必须原样下发给列表**，不能回调 `null` —— `null` 在服务层表示
 * 「不按码过滤」，会把「未分类」悄悄变成「全部失败」（一个超集）。
 */
const UNCLASSIFIED_KEY = '__unclassified__';

interface Props {
  rows: DoNl2SqlRow[];
  total: number;
  reasons: DoNl2SqlReasonCode[];
  loading: boolean;
  reasonCode: string | null;
  onReasonChange: (code: string | null) => void;
  onOpen: (row: DoNl2SqlRow) => void;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
}

/**
 * 失败复盘块（设计 §6.2）：上半失败码分布（点击按码过滤），下半失败明细。
 *
 * 分布来自 `overview.reason_codes`（全量口径），明细来自失败列表端点（已按码过滤）。
 * 因此点击某码后分布不会塌成只剩该码，用户可以继续在码之间切换；
 * 再点一次同一个码即清除筛选。
 */
export default function Nl2SqlFailurePanel({
  rows,
  total,
  reasons,
  loading,
  reasonCode,
  onReasonChange,
  onOpen,
  page,
  pageSize,
  onPageChange,
}: Props) {
  const { t } = useTranslation();

  const items: DoProportionItem[] = reasons.map((reason) =>
    reason.reason_code
      ? { key: reason.reason_code, label: reason.reason_code, count: reason.count }
      : {
          key: UNCLASSIFIED_KEY,
          labelKey: 'dataOperations.nl2sql.reasonCode.unclassified',
          count: reason.count,
        },
  );

  const activeKey = reasonCode ?? undefined;

  const handleReasonClick = (item: DoProportionItem) => {
    // 点当前生效的码 = 取消过滤，避免用户被「只能选一个」困住。
    // 空码桶同样按这条规则走：key 就是后端认识的 `__unclassified__`，
    // 选中时下发哨兵，再次点击才回 `null`（= 全部），两者不能混为一谈。
    onReasonChange(item.key === reasonCode ? null : item.key);
  };

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
      title: t('dataOperations.nl2sql.column.reasonCode'),
      dataIndex: 'reason_code',
      key: 'reason_code',
      width: 150,
      render: (value: string | null) => <ReasonCodeText code={value} />,
    },
    {
      title: t('dataOperations.nl2sql.column.a1b'),
      dataIndex: 'precompile_retried',
      key: 'precompile_retried',
      width: 100,
      render: (value: boolean) => <A1bTag retried={value} />,
    },
    {
      title: t('dataOperations.nl2sql.column.reason'),
      dataIndex: 'reason',
      key: 'reason',
      render: (value: string | null | undefined) => (
        <span title={value ?? undefined}>{value ? truncateText(value, 80) : NL2SQL_DASH}</span>
      ),
    },
  ];

  return (
    <div className="yx-do-nl2sql-failures">
      <ProportionList
        items={items}
        loading={loading && items.length === 0}
        renderValue={(item) => item.count.toLocaleString()}
        activeKey={activeKey}
        onItemClick={handleReasonClick}
      />

      <Table<DoNl2SqlRow>
        rowKey="id"
        size="small"
        style={{ marginTop: 16 }}
        columns={columns}
        dataSource={rows}
        loading={loading}
        locale={{ emptyText: t('dataOperations.nl2sql.empty.noFailures') }}
        onRow={(row) => ({
          onClick: () => onOpen(row),
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
    </div>
  );
}
