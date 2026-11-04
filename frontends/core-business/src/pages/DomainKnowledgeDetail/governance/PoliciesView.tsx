/** 治理 → 策略：列表 + 新建/编辑/删除。 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Empty, message, Popconfirm, Space, Spin, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  createGovernancePolicy, deleteGovernancePolicy,
  listGovernancePolicies, updateGovernancePolicy, type GovernancePolicy,
} from '@/api/governance';
import { EFFECT_TAGS, LEVEL_TAGS, tagFor } from './governanceModel';
import PolicyModal, { type GovernancePolicyForm } from './PolicyModal';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

function resourceText(p: GovernancePolicy): string {
  if (p.resource_type === 'tag') return `tag:${p.tag_name || ''}`;
  const parts = [p.table_pattern, p.column_pattern].filter(Boolean);
  return parts.join('.') || '—';
}

export default function PoliciesView({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<GovernancePolicy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<GovernancePolicy | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setItems((await listGovernancePolicies(kbId)).items);
    } catch (e: any) {
      setError(e?.message || t('domainKnowledge.governance.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => { void load(); }, [load]);

  const close = () => { setModalOpen(false); setEditing(null); };

  const submit = async (v: GovernancePolicyForm) => {
    setSubmitting(true);
    try {
      if (editing) {
        await updateGovernancePolicy(kbId, editing.id, v as any);
      } else {
        await createGovernancePolicy(kbId, v as any);
      }
      close();
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.governance.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const remove = async (row: GovernancePolicy) => {
    setDeletingId(row.id);
    try {
      await deleteGovernancePolicy(kbId, row.id);
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.governance.deleteFailed'));
    } finally {
      setDeletingId(null);
    }
  };

  const columns: ColumnsType<GovernancePolicy> = [
    { title: t('domainKnowledge.governance.policyName'), dataIndex: 'name', key: 'name' },
    {
      title: t('domainKnowledge.governance.effect'), dataIndex: 'effect', key: 'effect', width: 120,
      render: (effect: string) => {
        const meta = tagFor(EFFECT_TAGS, effect);
        return <Tag color={meta.color}>{t(meta.key)}</Tag>;
      },
    },
    {
      title: t('domainKnowledge.governance.resource'), key: 'resource',
      render: (_: unknown, row) => <code>{resourceText(row)}</code>,
    },
    {
      title: t('domainKnowledge.governance.minClearance'), dataIndex: 'min_clearance', key: 'mc',
      width: 130,
      render: (lv: string) => {
        const meta = tagFor(LEVEL_TAGS, lv);
        return <Tag color={meta.color}>{t(meta.key)}</Tag>;
      },
    },
    { title: t('domainKnowledge.governance.priority'), dataIndex: 'priority', key: 'priority', width: 90 },
    {
      title: t('domainKnowledge.governance.enabled'), dataIndex: 'enabled', key: 'enabled', width: 90,
      render: (v: boolean) => (v ? <Tag color="green">{t('domainKnowledge.governance.on')}</Tag>
        : <Tag>{t('domainKnowledge.governance.off')}</Tag>),
    },
  ];
  if (canWrite) {
    columns.push({
      title: t('domainKnowledge.lineage.actions'), key: 'actions', width: 150,
      render: (_: unknown, row) => (
        <Space size={0}>
          <Button type="text" size="small" icon={<EditOutlined />}
                  onClick={() => { setEditing(row); setModalOpen(true); }}>
            {t('common.edit')}
          </Button>
          <Popconfirm title={t('domainKnowledge.governance.deleteConfirm')}
                      onConfirm={() => void remove(row)}
                      okText={t('common.confirm')} cancelText={t('common.cancel')}>
            <Button type="text" size="small" danger icon={<DeleteOutlined />}
                    loading={deletingId === row.id}>
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    });
  }

  if (loading) return <div style={{ padding: 24, textAlign: 'center' }}><Spin /></div>;
  if (error) {
    return <Alert type="error" showIcon
                  message={t('domainKnowledge.governance.loadFailed')} description={error}
                  action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>} />;
  }

  return (
    <div>
      <div className="yx-kb-flex-header" style={{ marginBottom: 12 }}>
        <p className="yx-kb-graph-desc" style={{ margin: 0 }}>
          {t('domainKnowledge.governance.policiesHint')}
        </p>
        {canWrite && (
          <Button type="primary" icon={<PlusOutlined />}
                  onClick={() => { setEditing(null); setModalOpen(true); }}>
            {t('domainKnowledge.governance.addPolicy')}
          </Button>
        )}
      </div>
      {items.length === 0 ? (
        <Empty description={t('domainKnowledge.governance.policiesEmpty')} />
      ) : (
        <Table<GovernancePolicy> rowKey="id" columns={columns} dataSource={items}
                                 pagination={{ pageSize: 20, hideOnSinglePage: true }}
                                 size="middle" />
      )}
      {canWrite && (
        <PolicyModal open={modalOpen} policy={editing} submitting={submitting}
                     onCancel={close} onSubmit={(v) => void submit(v)} />
      )}
    </div>
  );
}
