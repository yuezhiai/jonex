import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Empty, Input, Select, Space, Table, Tag,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import { listQualityDataSourceOptions, listQualityIssues } from '@/api/quality';
import { listUsers } from '@/api/users';
import type {
  DgQualityIssue,
  DgQualityIssueListParams,
} from '../types';
import {
  QUALITY_ISSUE_STATUSES,
  QUALITY_SEVERITIES,
  severityColor,
  severityLabelKey,
  statusColor,
  statusLabelKey,
} from '../qualityUtils';
import QualityIssueDrawer from './QualityIssueDrawer';

interface Props {
  canWrite: boolean;
  userId: string | null;
  isAdmin: boolean;
}

interface Option {
  value: string;
  label: string;
}

const PAGE_SIZE = 20;

export default function QualityIssuesPanel({ canWrite, userId, isAdmin }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<DgQualityIssue[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<DgQualityIssueListParams['status']>('');
  const [severity, setSeverity] = useState<DgQualityIssueListParams['severity']>('');
  const [keyword, setKeyword] = useState('');
  const [dataSourceId, setDataSourceId] = useState('');
  const [assetType, setAssetType] = useState<DgQualityIssueListParams['assetType']>('');
  const [assigneeId, setAssigneeId] = useState('');
  const [dataSourceOptions, setDataSourceOptions] = useState<Option[]>([]);
  const [userOptions, setUserOptions] = useState<Option[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerIssueId, setDrawerIssueId] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await listQualityIssues({
        page, pageSize: PAGE_SIZE,
        ...(status ? { status } : {}),
        ...(severity ? { severity } : {}),
        ...(keyword ? { keyword } : {}),
        ...(dataSourceId ? { dataSourceId } : {}),
        ...(assetType ? { assetType } : {}),
        ...(assigneeId ? { assigneeId } : {}),
      });
      if (seq !== requestSeq.current) return;
      setItems(result.items);
      setTotal(result.total);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(err instanceof Error ? err.message : t('dataGovernance.quality.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [assetType, assigneeId, dataSourceId, keyword, page, severity, status, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void listQualityDataSourceOptions().then((options) => {
      setDataSourceOptions(options.map((item) => ({ value: item.id, label: item.name })));
    });
    void listUsers(1, 100)
      .then((result) => {
        setUserOptions(result.items.map((user) => ({
          value: String(user.id),
          label: user.display_name
            ? `${user.display_name} (${user.username})`
            : user.username,
        })));
      })
      .catch(() => setUserOptions([]));
  }, []);

  const userLabel = (value: string | null) => (
    userOptions.find((option) => option.value === value)?.label ?? value ?? '-'
  );

  const columns: ColumnsType<DgQualityIssue> = [
    { title: t('dataGovernance.quality.issue.title'), dataIndex: 'title', key: 'title', ellipsis: true },
    { title: t('dataGovernance.quality.issue.asset'), dataIndex: 'assetId', key: 'assetId', ellipsis: true },
    {
      title: t('dataGovernance.quality.issue.severity'),
      dataIndex: 'severity',
      key: 'severity',
      render: (value: string) => (
        <Tag color={severityColor(value)}>{t(severityLabelKey(value))}</Tag>
      ),
    },
    {
      title: t('dataGovernance.quality.issue.status'),
      dataIndex: 'status',
      key: 'status',
      render: (value: string) => (
        <Tag color={statusColor(value)}>{t(statusLabelKey(value))}</Tag>
      ),
    },
    {
      title: t('dataGovernance.quality.issue.assignee'),
      dataIndex: 'assigneeId',
      key: 'assigneeId',
      render: (value: string | null) => userLabel(value),
    },
    { title: t('dataGovernance.quality.issue.updatedAt'), dataIndex: 'updatedAt', key: 'updatedAt' },
    {
      title: t('dataGovernance.quality.issue.actions'),
      key: 'actions',
      render: (_, row) => (
        <Button type="link" size="small" onClick={() => setDrawerIssueId(row.id)}>
          {t('common.view')}
        </Button>
      ),
    },
  ];

  return (
    <div className="yx-dg-page">
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder={t('dataGovernance.quality.issue.filterDataSource')}
          value={dataSourceId || undefined}
          style={{ width: 190 }}
          options={dataSourceOptions}
          onChange={(value) => { setDataSourceId(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.issue.filterAssetType')}
          value={assetType || undefined}
          style={{ width: 130 }}
          options={[
            { value: 'table', label: t('dataGovernance.quality.rule.table') },
            { value: 'column', label: t('dataGovernance.quality.rule.column') },
          ]}
          onChange={(value) => { setAssetType(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder={t('dataGovernance.quality.issue.filterAssignee')}
          value={assigneeId || undefined}
          style={{ width: 190 }}
          options={userOptions}
          onChange={(value) => { setAssigneeId(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.issue.status')}
          value={status || undefined}
          style={{ width: 160 }}
          options={QUALITY_ISSUE_STATUSES.map((value) => ({
            value, label: t(statusLabelKey(value)),
          }))}
          onChange={(value) => { setStatus(value ?? ''); setPage(1); }}
        />
        <Select
          allowClear
          placeholder={t('dataGovernance.quality.issue.severity')}
          value={severity || undefined}
          style={{ width: 130 }}
          options={QUALITY_SEVERITIES.map((value) => ({
            value, label: t(severityLabelKey(value)),
          }))}
          onChange={(value) => { setSeverity(value ?? ''); setPage(1); }}
        />
        <Input.Search
          allowClear
          placeholder={t('dataGovernance.quality.issue.keyword')}
          style={{ width: 220 }}
          onSearch={(value) => { setKeyword(value.trim()); setPage(1); }}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          {t('common.refresh')}
        </Button>
      </Space>

      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>}
        />
      ) : (
        <Table<DgQualityIssue>
          rowKey="id"
          columns={columns}
          dataSource={items}
          loading={loading}
          size="middle"
          scroll={{ x: 1100 }}
          locale={{ emptyText: loading ? null : <Empty description={t('dataGovernance.quality.empty')} /> }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            onChange: setPage,
            showTotal: (n) => t('dataGovernance.total', { total: n }),
          }}
        />
      )}

      <QualityIssueDrawer
        open={drawerIssueId !== null}
        issueId={drawerIssueId}
        userId={userId}
        isAdmin={isAdmin}
        onClose={() => setDrawerIssueId(null)}
        onChanged={() => void load()}
      />
    </div>
  );
}
