/**
 * W2.6 落标视图：覆盖率汇总 + 标准列表 + 绑定抽屉 + 建议候选。
 *
 * 绑定写操作仅租户管理员；乐观锁冲突（1003）提示刷新后重试。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert, Button, Card, Empty, Modal, Select, Space, Statistic, Table, Tag, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, ReloadOutlined, BulbOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';

import {
  createStandardBinding,
  getStandardBindingCoverage,
  suggestStandardBindings,
} from '@/api/resourceViews';
import type {
  DgBindingSuggestion,
  DgCoverageResult,
  DgCoverageRow,
  DgStandardAssetKind,
} from '../types';
import {
  coveragePercent,
} from '../resourceViewUtils';
import { errorMeta } from '../utils';
import { kindLabelKey } from '../standardAssetUtils';
import {
  BindingDrawer,
  BindingListDrawer,
  type BindingTarget,
} from './BindingDrawer';

interface Props {
  canWrite: boolean;
}

const PAGE_SIZE = 20;
const SUGGESTION_LIMIT = 50;

function isConflict(err: unknown): boolean {
  return errorMeta(err).bizCode === 1003;
}

function standardStatusLabel(
  status: string,
  t: (key: string) => string,
): string {
  return ['draft', 'published', 'retired'].includes(status)
    ? t(`dataGovernance.standardAssets.status.${status}`)
    : status;
}

export default function LandingViewPanel({ canWrite }: Props) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<DgStandardAssetKind | ''>('');
  const [page, setPage] = useState(1);
  const [data, setData] = useState<DgCoverageResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [bindingsTarget, setBindingsTarget] = useState<BindingTarget | null>(null);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<DgBindingSuggestion[]>([]);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestError, setSuggestError] = useState<string | null>(null);
  const [adoptingKey, setAdoptingKey] = useState<string | null>(null);
  const requestSeq = useRef(0);
  const suggestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    try {
      const result = await getStandardBindingCoverage({
        ...(kind ? { kind } : {}),
        page,
        pageSize: PAGE_SIZE,
      });
      if (seq !== requestSeq.current) return;
      setData(result);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setData(null);
      setError(err instanceof Error && err.message
        ? err.message
        : t('dataGovernance.resourceViews.landing.loadFailed'));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [kind, page, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const openSuggestions = useCallback(async () => {
    setSuggestOpen(true);
    const seq = ++suggestSeq.current;
    setSuggestLoading(true);
    setSuggestError(null);
    try {
      const items = await suggestStandardBindings({ limit: SUGGESTION_LIMIT });
      if (seq !== suggestSeq.current) return;
      setSuggestions(items);
    } catch (err) {
      if (seq !== suggestSeq.current) return;
      setSuggestions([]);
      setSuggestError(err instanceof Error && err.message
        ? err.message
        : t('dataGovernance.resourceViews.landing.suggestions.loadFailed'));
    } finally {
      if (seq === suggestSeq.current) setSuggestLoading(false);
    }
  }, [t]);

  const suggestionKey = (item: DgBindingSuggestion) =>
    `${item.standardKind}:${item.standardCode}:${item.assetType}:${item.assetId}`;

  const adopt = async (item: DgBindingSuggestion) => {
    if (!canWrite) return;
    const key = suggestionKey(item);
    setAdoptingKey(key);
    try {
      await createStandardBinding({
        standardKind: item.standardKind,
        standardCode: item.standardCode,
        assetType: item.assetType,
        assetId: item.assetId,
        source: 'suggested',
        ...(item.confidence !== null ? { confidence: item.confidence } : {}),
      });
      message.success(t('dataGovernance.resourceViews.landing.suggestions.adopted'));
      setSuggestions((prev) => prev.filter((row) => suggestionKey(row) !== key));
      void load();
    } catch (err) {
      message.error(
        isConflict(err)
          ? t('dataGovernance.resourceViews.binding.conflict')
          : (err instanceof Error && err.message
            ? err.message
            : t('dataGovernance.resourceViews.landing.suggestions.adoptFailed')),
      );
      if (isConflict(err)) void load();
    } finally {
      setAdoptingKey(null);
    }
  };

  const ignore = (item: DgBindingSuggestion) => {
    const key = suggestionKey(item);
    setSuggestions((prev) => prev.filter((row) => suggestionKey(row) !== key));
    message.info(t('dataGovernance.resourceViews.landing.suggestions.ignored'));
  };

  const summary = data?.summary;

  const columns: ColumnsType<DgCoverageRow> = [
    {
      title: t('dataGovernance.resourceViews.landing.standard'),
      key: 'standard',
      width: 260,
      ellipsis: true,
      render: (_value, row) => (
        <Space direction="vertical" size={0}>
          <span>{row.name || row.standardCode}</span>
          <span className="yx-dg-muted">{row.standardCode}</span>
        </Space>
      ),
    },
    {
      title: t('dataGovernance.standardAssets.kindLabel'),
      dataIndex: 'standardKind',
      width: 120,
      render: (value: string) => <Tag>{t(kindLabelKey(value))}</Tag>,
    },
    {
      title: t('dataGovernance.standardAssets.version'),
      dataIndex: 'version',
      width: 80,
      render: (value: number) => `v${value}`,
    },
    {
      title: t('dataGovernance.resourceViews.landing.domain'),
      dataIndex: 'domain',
      width: 140,
      ellipsis: true,
      render: (value: string | null) => value || <span className="yx-dg-muted">—</span>,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      width: 110,
      render: (value: string) => <Tag>{standardStatusLabel(value, (key) => t(key))}</Tag>,
    },
    {
      title: t('dataGovernance.resourceViews.landing.bindingsCount'),
      dataIndex: 'bindingsCount',
      width: 120,
      align: 'right',
    },
    {
      title: t('dataGovernance.resourceViews.landing.coverageState'),
      key: 'coverageState',
      width: 110,
      render: (_value, row) => (row.unbound
        ? <Tag color="orange">{t('dataGovernance.resourceViews.landing.unbound')}</Tag>
        : <Tag color="green">{t('dataGovernance.resourceViews.landing.bound')}</Tag>),
    },
    {
      title: t('dataGovernance.resourceViews.landing.actions'),
      key: 'actions',
      width: 130,
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          onClick={() => setBindingsTarget({
            kind: row.standardKind,
            code: row.standardCode,
            name: row.name,
          })}
        >
          {t('dataGovernance.resourceViews.landing.viewBindings')}
        </Button>
      ),
    },
  ];

  const suggestionColumns: ColumnsType<DgBindingSuggestion> = [
    {
      title: t('dataGovernance.resourceViews.landing.suggestions.standard'),
      key: 'standard',
      width: 230,
      ellipsis: true,
      render: (_value, row) => {
        const kindKey = kindLabelKey(row.standardKind);
        const kindText = kindKey === row.standardKind ? row.standardKind : t(kindKey);
        return `${kindText} · ${row.standardCode} · ${row.standardName}`;
      },
    },
    {
      title: t('dataGovernance.resourceViews.landing.suggestions.asset'),
      key: 'asset',
      width: 220,
      ellipsis: true,
      render: (_value, row) => (
        <Space direction="vertical" size={0}>
          <span>{row.assetName || row.assetId}</span>
          <span className="yx-dg-muted">
            {t(`dataGovernance.resourceViews.assetType.${row.assetType}`)}
            {' · '}
            {row.assetId}
          </span>
        </Space>
      ),
    },
    {
      title: t('dataGovernance.resourceViews.landing.suggestions.confidence'),
      dataIndex: 'confidence',
      width: 100,
      align: 'right',
      render: (value: number | null) =>
        (typeof value === 'number' ? `${Math.round(value * 1000) / 10}%` : '—'),
    },
    {
      title: t('dataGovernance.resourceViews.landing.suggestions.reason'),
      dataIndex: 'reason',
      ellipsis: true,
      render: (value: string | null) => value || '—',
    },
    {
      title: t('dataGovernance.resourceViews.landing.actions'),
      key: 'actions',
      width: 150,
      render: (_value, row) => {
        const key = suggestionKey(row);
        return (
          <Space>
            {canWrite ? (
              <Button
                type="link"
                size="small"
                loading={adoptingKey === key}
                onClick={() => void adopt(row)}
              >
                {t('dataGovernance.resourceViews.landing.suggestions.adopt')}
              </Button>
            ) : null}
            <Button type="link" size="small" onClick={() => ignore(row)}>
              {t('dataGovernance.resourceViews.landing.suggestions.ignore')}
            </Button>
          </Space>
        );
      },
    },
  ];

  return (
    <div className="yx-dg-page">
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          allowClear
          placeholder={t('dataGovernance.resourceViews.landing.kindFilter')}
          value={kind || undefined}
          style={{ width: 170 }}
          options={(['term', 'data_element', 'metric_card'] as DgStandardAssetKind[]).map(
            (value) => ({ value, label: t(kindLabelKey(value)) }),
          )}
          onChange={(value) => {
            setKind(value ?? '');
            setPage(1);
          }}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>
          {t('common.refresh')}
        </Button>
        <Button icon={<BulbOutlined />} onClick={() => void openSuggestions()}>
          {t('dataGovernance.resourceViews.landing.suggestions.open')}
        </Button>
        {canWrite ? (
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            {t('dataGovernance.resourceViews.binding.createTitle')}
          </Button>
        ) : null}
      </Space>

      {summary ? (
        <Space wrap style={{ marginBottom: 12 }}>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.landing.totalStandards')}
              value={summary.totalStandards}
            />
          </Card>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.landing.boundStandards')}
              value={summary.boundStandards}
            />
          </Card>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.landing.bindingCount')}
              value={summary.bindingCount}
            />
          </Card>
          <Card size="small">
            <Statistic
              title={t('dataGovernance.resourceViews.landing.coverage')}
              value={coveragePercent(summary.coverage)}
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
        <Table<DgCoverageRow>
          rowKey="standardAssetId"
          columns={columns}
          dataSource={data?.items ?? []}
          loading={loading}
          size="middle"
          scroll={{ x: 1200 }}
          locale={{
            emptyText: loading
              ? null
              : <Empty description={t('dataGovernance.resourceViews.landing.empty')} />,
          }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total: data?.total ?? 0,
            onChange: setPage,
            showTotal: (n) => t('dataGovernance.total', { total: n }),
          }}
        />
      )}

      <BindingDrawer
        open={createOpen}
        canWrite={canWrite}
        onClose={() => setCreateOpen(false)}
        onSaved={() => void load()}
      />

      <BindingListDrawer
        open={Boolean(bindingsTarget)}
        standard={bindingsTarget}
        canWrite={canWrite}
        onClose={() => setBindingsTarget(null)}
        onChanged={() => void load()}
      />

      <Modal
        open={suggestOpen}
        title={t('dataGovernance.resourceViews.landing.suggestions.title')}
        width={1000}
        footer={null}
        onCancel={() => setSuggestOpen(false)}
      >
        {suggestError ? (
          <Alert
            type="error"
            showIcon
            message={suggestError}
            action={(
              <Button size="small" onClick={() => void openSuggestions()}>
                {t('common.retry')}
              </Button>
            )}
          />
        ) : (
          <Table<DgBindingSuggestion>
            rowKey={suggestionKey}
            columns={suggestionColumns}
            dataSource={suggestions}
            loading={suggestLoading}
            size="small"
            scroll={{ x: 900 }}
            pagination={false}
            locale={{
              emptyText: suggestLoading
                ? null
                : (
                  <Empty
                    description={t('dataGovernance.resourceViews.landing.suggestions.empty')}
                  />
                ),
            }}
          />
        )}
      </Modal>
    </div>
  );
}
