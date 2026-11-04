/**
 * W2.6 业务域视图：空间 / 域筛选、五类资产计数、责任人与分级汇总。
 *
 * 点击分组行下钻资产目录（把空间与域作为筛选条件带回 assets 页签）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Card, Empty, Input, Select, Space, Statistic, Table, Tag, Tooltip,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import { listKnowledgeBaseSpaces } from '@/api/knowledgeBase';
import { getDomainView } from '@/api/resourceViews';
import type { DgDomainViewGroup, DgDomainViewResult } from '../types';
import { UNASSIGNED_DOMAIN, domainLabel } from '../resourceViewUtils';

interface Option {
  value: string;
  label: string;
}

interface Props {
  onDrillDown: (filters: { spaceId?: string; domain?: string }) => void;
}

const EMPTY_RESULT: DgDomainViewResult | null = null;

function issueTotal(group: DgDomainViewGroup): number {
  return (
    group.openIssues.critical
    + group.openIssues.error
    + group.openIssues.warning
    + group.openIssues.info
  );
}

export default function DomainViewPanel({ onDrillDown }: Props) {
  const { t } = useTranslation();
  const [spaces, setSpaces] = useState<Option[]>([]);
  const [spaceId, setSpaceId] = useState('');
  const [domainDraft, setDomainDraft] = useState('');
  const [domain, setDomain] = useState('');
  const [data, setData] = useState<DgDomainViewResult | null>(EMPTY_RESULT);
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
      const result = await getDomainView({
        ...(spaceId ? { spaceId } : {}),
        ...(domain ? { domain } : {}),
      });
      if (seq !== requestSeq.current) return;
      setData(result);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setData(null);
      setError(err instanceof Error && err.message
        ? err.message
        : t('dataGovernance.resourceViews.domains.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [spaceId, domain, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const applyDomain = useCallback(() => {
    setDomain(domainDraft.trim());
  }, [domainDraft]);

  const resetFilters = useCallback(() => {
    setSpaceId('');
    setDomainDraft('');
    setDomain('');
  }, []);

  const drill = (row: DgDomainViewGroup) => {
    onDrillDown({
      ...(row.spaceId ? { spaceId: row.spaceId } : {}),
      // 未分配域在资产目录是空值，传空串等于不筛选；不伪造 `__unassigned__`。
      ...(row.domain && row.domain !== UNASSIGNED_DOMAIN ? { domain: row.domain } : {}),
    });
  };

  const columns: ColumnsType<DgDomainViewGroup> = [
    {
      title: t('dataGovernance.resourceViews.domains.space'),
      key: 'space',
      width: 180,
      ellipsis: true,
      render: (_value, row) => row.spaceName || row.spaceId || '—',
    },
    {
      title: t('dataGovernance.resourceViews.domains.domain'),
      dataIndex: 'domain',
      width: 180,
      ellipsis: true,
      render: (value: string) => {
        const label = domainLabel(value);
        const isUnassigned = !value || value === UNASSIGNED_DOMAIN;
        return isUnassigned
          ? <Tag>{t(label)}</Tag>
          : <span>{label}</span>;
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
      title: t('dataGovernance.resourceViews.domains.owners'),
      key: 'owners',
      width: 200,
      render: (_value, row) => {
        if (!row.owners.length) {
          return <span className="yx-dg-muted">{t('dataGovernance.unset')}</span>;
        }
        const names = row.owners
          .slice(0, 3)
          .map((item) => `${item.owner} (${item.count})`)
          .join(', ');
        return (
          <Tooltip title={row.owners.map((item) => `${item.owner} (${item.count})`).join(', ')}>
            <span>
              {names}
              {row.owners.length > 3 ? '…' : ''}
            </span>
          </Tooltip>
        );
      },
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
      width: 150,
      render: (_value, row) => {
        const total = issueTotal(row);
        if (total === 0) return <span className="yx-dg-muted">0</span>;
        return (
          <Tooltip
            title={[
              t('dataGovernance.quality.severity.critical'),
              t('dataGovernance.quality.severity.error'),
              t('dataGovernance.quality.severity.warning'),
              t('dataGovernance.quality.severity.info'),
            ].map((label, index) => {
              const counts = [
                row.openIssues.critical,
                row.openIssues.error,
                row.openIssues.warning,
                row.openIssues.info,
              ];
              return `${label}: ${counts[index]}`;
            }).join(', ')}
          >
            <Tag color={row.openIssues.critical > 0 ? 'red' : 'orange'}>{total}</Tag>
          </Tooltip>
        );
      },
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
        <Input
          allowClear
          placeholder={t('dataGovernance.resourceViews.filter.domain')}
          value={domainDraft}
          style={{ width: 180 }}
          onChange={(event) => setDomainDraft(event.target.value)}
          onPressEnter={applyDomain}
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={applyDomain}>
          {t('dataGovernance.filter.search')}
        </Button>
        <Button onClick={resetFilters}>{t('dataGovernance.filter.reset')}</Button>
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
              title={t('dataGovernance.resourceViews.domains.ownerCount')}
              value={totals.ownerCount}
            />
          </Card>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.assetType.table')}
              value={totals.assetCounts.table}
            />
          </Card>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.assetType.column')}
              value={totals.assetCounts.column}
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
        <Table<DgDomainViewGroup>
          rowKey={(row) => `${row.spaceId ?? ''}:${row.domain}`}
          columns={columns}
          dataSource={data?.groups ?? []}
          loading={loading}
          size="middle"
          scroll={{ x: 1500 }}
          locale={{
            emptyText: loading
              ? null
              : <Empty description={t('dataGovernance.resourceViews.domains.empty')} />,
          }}
          pagination={false}
          onRow={(row) => ({
            onClick: () => drill(row),
            style: { cursor: 'pointer' },
          })}
        />
      )}
    </div>
  );
}
