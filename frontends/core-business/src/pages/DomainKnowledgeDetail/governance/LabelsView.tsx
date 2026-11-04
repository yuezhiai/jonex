/** 治理 → 资产标签：列表 + 人工编辑（表级/列级）。 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Empty, message, Popconfirm, Space, Spin, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getDomainKnowledgeDataSources } from '@/api/domainKnowledge';
import {
  createGovernanceLabel, deleteGovernanceLabel,
  listGovernanceLabels, updateGovernanceLabel, type GovernanceLabel,
} from '@/api/governance';
import type { DataSourceConfig } from '@/types/domainKnowledge';
import { LEVEL_TAGS, tagFor } from './governanceModel';
import LabelModal, { type GovernanceLabelForm } from './LabelModal';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

export default function LabelsView({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [sources, setSources] = useState<DataSourceConfig[]>([]);
  const [items, setItems] = useState<GovernanceLabel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<GovernanceLabel | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await listGovernanceLabels(kbId);
      setItems(data.items);
    } catch (e: any) {
      setError(e?.message || t('domainKnowledge.governance.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => {
    void load();
    void getDomainKnowledgeDataSources(kbId, t)
      .then((list) => setSources(list.filter((s) => s.accessType === 'database')))
      .catch(() => setSources([]));
  }, [kbId, load, t]);

  const submit = async (v: GovernanceLabelForm) => {
    setSubmitting(true);
    try {
      if (editing) {
        await updateGovernanceLabel(kbId, editing.id, {
          level: v.level as GovernanceLabel['level'],
          tags: v.tags, note: v.note,
        });
      } else {
        await createGovernanceLabel(kbId, {
          data_source_id: v.data_source_id!,
          table_name: v.table_name!,
          column_name: v.column_name || null,
          level: v.level as GovernanceLabel['level'],
          tags: v.tags, note: v.note,
        } as any);
      }
      close();
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.governance.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const close = () => {
    setModalOpen(false);
    setEditing(null);
  };

  const remove = async (row: GovernanceLabel) => {
    setDeletingId(row.id);
    try {
      await deleteGovernanceLabel(kbId, row.id);
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.governance.deleteFailed'));
    } finally {
      setDeletingId(null);
    }
  };

  const columns: ColumnsType<GovernanceLabel> = [
    { title: t('domainKnowledge.governance.tableName'), dataIndex: 'table_name', key: 'table' },
    {
      title: t('domainKnowledge.governance.columnName'), dataIndex: 'column_name', key: 'column',
      render: (v?: string | null) => v || '—',
    },
    {
      title: t('domainKnowledge.governance.level'), dataIndex: 'level', key: 'level', width: 120,
      render: (level: string) => {
        const meta = tagFor(LEVEL_TAGS, level);
        return <Tag color={meta.color}>{t(meta.key)}</Tag>;
      },
    },
    {
      title: t('domainKnowledge.governance.tags'), dataIndex: 'tags', key: 'tags',
      render: (tags: string[]) => (tags?.length
        ? tags.map((tag) => <Tag key={tag}>{tag}</Tag>)
        : <span style={{ color: '#cbd5e1' }}>—</span>),
    },
    {
      title: t('domainKnowledge.governance.origin'), dataIndex: 'origin', key: 'origin', width: 110,
      render: (origin: string) => <Tag>{t(`domainKnowledge.governance.origin${origin[0].toUpperCase()}${origin.slice(1)}`)}</Tag>,
    },
    { title: t('domainKnowledge.governance.note'), dataIndex: 'note', key: 'note' },
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
          {t('domainKnowledge.governance.labelsHint')}
        </p>
        {canWrite && (
          <Button type="primary" icon={<PlusOutlined />}
                  onClick={() => { setEditing(null); setModalOpen(true); }}>
            {t('domainKnowledge.governance.addLabel')}
          </Button>
        )}
      </div>
      {items.length === 0 ? (
        <Empty description={t('domainKnowledge.governance.labelsEmpty')} />
      ) : (
        <Table<GovernanceLabel> rowKey="id" columns={columns} dataSource={items}
                                pagination={{ pageSize: 20, hideOnSinglePage: true }}
                                size="middle" />
      )}
      {canWrite && (
        <LabelModal open={modalOpen} label={editing} sources={sources}
                    submitting={submitting} onCancel={close}
                    onSubmit={(v) => void submit(v)} />
      )}
    </div>
  );
}
