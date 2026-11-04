/**
 * W2.6 责任视图：按 owner / domain 分组，未分配分组高亮并可下钻资产目录。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Card, Empty, Segmented, Select, Space, Statistic, Table, Tag, Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { formatDateTime } from '@jonex/shared-lib';

import { listKnowledgeBaseSpaces } from '@/api/knowledgeBase';
import { getResponsibilityView } from '@/api/resourceViews';
import type {
  DgResponsibilityGroupBy,
  DgResponsibilityResult,
  DgResponsibilityRow,
} from '../types';
import {
  UNASSIGNED_DOMAIN,
  UNOWNED_OWNER,
  domainLabel,
  ownerLabel,
} from '../resourceViewUtils';

interface Option {
  value: string;
  label: string;
}

interface Props {
  onDrillDown: (filters: { spaceId?: string; owner?: string; domain?: string }) => void;
}

function issueTotal(row: DgResponsibilityRow): number {
  return (
    row.openIssues.critical
    + row.openIssues.error
    + row.openIssues.warning
    + row.openIssues.info
  );
}

export default function ResponsibilityPanel({ onDrillDown }: Props) {
  const { t } = useTranslation();
  const [spaces, setSpaces] = useState<Option[]>([]);
  const [spaceId, setSpaceId] = useState('');
  const [groupBy, setGroupBy] = useState<DgResponsibilityGroupBy>('owner');
  const [data, setData] = useState<DgResponsibilityResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  useEffect(() => {
    let cancelled = false;
    void listKnowledgeBaseSpaces()
      .then((items) => {
        if (cancelled) return;
        setSpaces(items.map((item) => ({ value: item.id, label: item.name })));
      })
      .catch(() => {
        if (!cancelled) setSpaces([]);
      });
    return () => { cancelled = true; };
  }, []);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await getResponsibilityView({
        groupBy,
        ...(spaceId ? { spaceId } : {}),
      });
      if (seq !== requestSeq.current) return;
      setData(result);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setData(null);
      setError(err instanceof Error && err.message
        ? err.message
        : t('dataGovernance.resourceViews.responsibilities.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [groupBy, spaceId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const isUnassigned = (row: DgResponsibilityRow) =>
    groupBy === 'owner'
      ? !row.key || row.key === UNOWNED_OWNER
      : !row.key || row.key === UNASSIGNED_DOMAIN;

  const drill = (row: DgResponsibilityRow) => {
    if (groupBy === 'owner') {
      onDrillDown({
        ...(spaceId ? { spaceId } : {}),
        // 未指定责任人在资产目录是空值，传空等于不筛选。
        ...(row.key && row.key !== UNOWNED_OWNER ? { owner: row.key } : {}),
      });
      return;
    }
    onDrillDown({
      ...(spaceId ? { spaceId } : {}),
      ...(row.key && row.key !== UNASSIGNED_DOMAIN ? { domain: row.key } : {}),
    });
  };

  const columns: ColumnsType<DgResponsibilityRow> = [
    {
      title: groupBy === 'owner'
        ? t('dataGovernance.resourceViews.responsibilities.owner')
        : t('dataGovernance.resourceViews.responsibilities.domain'),
      key: 'subject',
      width: 220,
      ellipsis: true,
      render: (_value, row) => {
        const label = groupBy === 'owner'
          ? ownerLabel(row.key, row.displayName)
          : domainLabel(row.key);
        if (isUnassigned(row)) {
          return <Tag color="orange">{t(label)}</Tag>;
        }
        return <span>{row.displayName || row.key}</span>;
      },
    },
    {
      title: t('dataGovernance.resourceViews.assetType.table'),
      dataIndex: ['assetCounts', 'table'],
      width: 90,
      align: 'right',
    },
    {
      title: t('dataGovernance.resourceViews.assetType.column'),
      dataIndex: ['assetCounts', 'column'],
      width: 90,
      align: 'right',
    },
    {
      title: t('dataGovernance.resourceViews.assetType.metric'),
      dataIndex: ['assetCounts', 'metric'],
      width: 90,
      align: 'right',
    },
    {
      title: t('dataGovernance.resourceViews.assetType.document'),
      dataIndex: ['assetCounts', 'document'],
      width: 100,
      align: 'right',
    },
    {
      title: t('dataGovernance.resourceViews.assetType.dataSource'),
      dataIndex: ['assetCounts', 'dataSource'],
      width: 100,
      align: 'right',
    },
    {
      title: t('dataGovernance.resourceViews.domains.sensitivity'),
      key: 'sensitivity',
      width: 190,
      render: (_value, row) => {
        const levels = Object.keys(row.sensitivity) as (keyof typeof row.sensitivity)[];
        if (levels.every((key) => row.sensitivity[key] === 0)) {
          return <span className="yx-dg-muted">—</span>;
        }
        return (
          <Space size={[0, 4]} wrap>
            {levels
              .filter((key) => row.sensitivity[key] > 0)
              .map((key) => (
                <Tag key={key}>
                  {t(`dataGovernance.resourceViews.sensitivity.${key}`)}
                  {`: ${row.sensitivity[key]}`}
                </Tag>
              ))}
          </Space>
        );
      },
    },
    {
      title: t('dataGovernance.resourceViews.domains.openIssues'),
      key: 'openIssues',
      width: 120,
      render: (_value, row) => {
        const total = issueTotal(row);
        return total === 0
          ? <span className="yx-dg-muted">0</span>
          : <Tooltip title={[
              `${t('dataGovernance.quality.severity.critical')} ${row.openIssues.critical}`,
              `${t('dataGovernance.quality.severity.error')} ${row.openIssues.error}`,
              `${t('dataGovernance.quality.severity.warning')} ${row.openIssues.warning}`,
              `${t('dataGovernance.quality.severity.info')} ${row.openIssues.info}`,
            ].join(' / ')}>
            <Tag color={row.openIssues.critical > 0 ? 'red' : 'orange'}>{total}</Tag>
          </Tooltip>;
      },
    },
    {
      title: t('dataGovernance.resourceViews.responsibilities.lastUpdatedAt'),
      dataIndex: 'lastUpdatedAt',
      width: 170,
      render: (value: string | null) => (value ? formatDateTime(value) : '—'),
    },
    {
      title: t('dataGovernance.resourceViews.domains.action'),
      key: 'action',
      width: 110,
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          onClick={(event) => {
            event.stopPropagation();
            drill(row);
          }}
        >
          {t('dataGovernance.resourceViews.domains.viewAssets')}
        </Button>
      ),
    },
  ];

  const totals = data?.totals;

  return (
    <div className="yx-dg-page">
      <Space wrap style={{ marginBottom: 12 }}>
        <Segmented
          value={groupBy}
          options={[
            {
              value: 'owner',
              label: t('dataGovernance.resourceViews.responsibility.groupBy.owner'),
            },
            {
              value: 'domain',
              label: t('dataGovernance.resourceViews.responsibility.groupBy.domain'),
            },
          ]}
          onChange={(value) => setGroupBy(value as DgResponsibilityGroupBy)}
        />
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder={t('dataGovernance.resourceViews.filter.space')}
          value={spaceId || undefined}
          style={{ width: 190 }}
          options={spaces}
          onChange={(value) => setSpaceId(value ?? '')}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          {t('common.refresh')}
        </Button>
      </Space>

      {data?.truncated ? (
        <Alert
          type="warning"
          showIcon
          message={t('dataGovernance.resourceViews.truncatedHint')}
          style={{ marginBottom: 12 }}
        />
      ) : null}

      {totals ? (
        <Space wrap style={{ marginBottom: 12 }}>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.domains.groups')}
              value={totals.groups}
            />
          </Card>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.responsibilities.unowned')}
              value={totals.unowned}
            />
          </Card>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.responsibilities.unassignedDomain')}
              value={totals.unassignedDomain}
            />
          </Card>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.domains.openIssues')}
              value={
                totals.openIssues.critical
                + totals.openIssues.error
                + totals.openIssues.warning
                + totals.openIssues.info
              }
            />
          </Card>
        </Space>
      ) : null}

      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>}
        />
      ) : (
        <Table<DgResponsibilityRow>
          rowKey="key"
          columns={columns}
          dataSource={data?.rows ?? []}
          loading={loading}
          size="middle"
          scroll={{ x: 1500 }}
          locale={{
            emptyText: loading
              ? null
              : <Empty description={t('dataGovernance.resourceViews.responsibilities.empty')} />,
          }}
          pagination={false}
          rowClassName={(row) => (isUnassigned(row) ? 'yx-dg-resource-row-unassigned' : '')}
          onRow={(row) => ({
            onClick: () => drill(row),
            style: { cursor: 'pointer' },
          })}
        />
      )}
    </div>
  );
}
