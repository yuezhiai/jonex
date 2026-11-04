/** 治理 → 审计：判定记录（按决策/动作筛选）。 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Empty, Select, Space, Spin, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import { listGovernanceAudit, type GovernanceAudit } from '@/api/governance';
import { DECISION_TAGS, LEVEL_TAGS, tagFor } from './governanceModel';

interface Props { kbId: string }

export default function AuditView({ kbId }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<GovernanceAudit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [decision, setDecision] = useState<string | undefined>();
  const [action, setAction] = useState<string | undefined>();

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setItems((await listGovernanceAudit(kbId, { decision, action, limit: 200 })).items);
    } catch (e: any) {
      setError(e?.message || t('domainKnowledge.governance.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, decision, action, t]);

  useEffect(() => { void load(); }, [load]);

  const columns: ColumnsType<GovernanceAudit> = [
    {
      title: t('domainKnowledge.governance.time'), dataIndex: 'created_at', key: 'time',
      width: 190, render: (v?: string | null) => v || '—',
    },
    { title: t('domainKnowledge.governance.user'), dataIndex: 'user_id', key: 'user', width: 110 },
    { title: t('domainKnowledge.governance.action'), dataIndex: 'action', key: 'action', width: 110 },
    {
      title: t('domainKnowledge.governance.resource'), key: 'resource',
      render: (_: unknown, row) => (
        <code>{[row.resource_table, row.resource_column].filter(Boolean).join('.') || '—'}</code>
      ),
    },
    {
      title: t('domainKnowledge.governance.level'), dataIndex: 'level', key: 'level', width: 120,
      render: (level?: string | null) => {
        if (!level) return '—';
        const meta = tagFor(LEVEL_TAGS, level);
        return <Tag color={meta.color}>{t(meta.key)}</Tag>;
      },
    },
    {
      title: t('domainKnowledge.governance.decision'), dataIndex: 'decision', key: 'decision', width: 120,
      render: (d: string) => {
        const meta = tagFor(DECISION_TAGS, d);
        return <Tag color={meta.color}>{t(meta.key)}</Tag>;
      },
    },
    { title: t('domainKnowledge.governance.reason'), dataIndex: 'reason', key: 'reason' },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Select allowClear placeholder={t('domainKnowledge.governance.decision')}
                style={{ width: 160 }} value={decision} onChange={setDecision}
                options={['allow', 'deny', 'mask', 'row_filter'].map((d) => {
                  const meta = tagFor(DECISION_TAGS, d);
                  return { value: d, label: t(meta.key) };
                })} />
        <Select allowClear placeholder={t('domainKnowledge.governance.action')}
                style={{ width: 150 }} value={action} onChange={setAction}
                options={['select', 'predicate', 'aggregate'].map((a) => ({
                  value: a,
                  label: t(`domainKnowledge.governance.action${a[0].toUpperCase()}${a.slice(1)}`),
                }))} />
        <Button onClick={() => void load()}>{t('common.refresh')}</Button>
      </Space>
      {loading ? (
        <div style={{ padding: 24, textAlign: 'center' }}><Spin /></div>
      ) : error ? (
        <Alert type="error" showIcon
               message={t('domainKnowledge.governance.loadFailed')} description={error}
               action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>} />
      ) : items.length === 0 ? (
        <Empty description={t('domainKnowledge.governance.auditEmpty')} />
      ) : (
        <Table<GovernanceAudit> rowKey="id" columns={columns} dataSource={items}
                                pagination={{ pageSize: 20, hideOnSinglePage: true }}
                                size="middle" />
      )}
    </div>
  );
}
