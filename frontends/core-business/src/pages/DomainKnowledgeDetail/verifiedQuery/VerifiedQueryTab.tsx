/** 已验证查询（N1-1）：列表 + 新建/编辑/激活/退役/删除 + 匹配预览。 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, Card, Empty, Input, message, Popconfirm, Select, Space,
  Spin, Table, Tag, Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  DeleteOutlined, EditOutlined, PlayCircleOutlined, PlusOutlined,
  StopOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { listDataSources } from '@/api/dataSource';
import {
  activateVerifiedQuery, deleteVerifiedQuery, listVerifiedQueries,
  previewVerifiedMatch, retireVerifiedQuery, saveVerifiedQuery,
  type VerifiedQuery, type VerifiedQueryPreview, type VerifiedQuerySavePayload,
} from '@/api/verifiedQuery';
import VerifiedQueryFormModal from './VerifiedQueryFormModal';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

const STATUS_COLOR: Record<string, string> = {
  draft: 'default',
  active: 'green',
  retired: 'orange',
};

export default function VerifiedQueryTab({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<VerifiedQuery[]>([]);
  const [dataSources, setDataSources] = useState<{ id: string; name: string; accessType: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<VerifiedQuery | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [previewDs, setPreviewDs] = useState('');
  const [previewQuestion, setPreviewQuestion] = useState('');
  const [preview, setPreview] = useState<VerifiedQueryPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const dbSources = useMemo(
    () => dataSources.filter((d) => d.accessType === 'database'),
    [dataSources],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [res, ds] = await Promise.all([
        listVerifiedQueries(kbId),
        listDataSources(kbId),
      ]);
      setItems(res.items || []);
      setDataSources(ds.map((d) => ({ id: d.id, name: d.name, accessType: d.accessType })));
    } catch (e: any) {
      setError(e?.message || t('domainKnowledge.verifiedQuery.loadFailed'));
    } finally {
      setLoading(false);
    }
    // listDataSources 的返回类型在 map 后已转 camelCase
  }, [kbId, t]);

  useEffect(() => { void load(); }, [load]);

  const dsName = (id: string) => dataSources.find((d) => d.id === id)?.name || id;

  const submit = async (payload: Omit<VerifiedQuerySavePayload, 'knowledge_base_id'>) => {
    setSubmitting(true);
    try {
      await saveVerifiedQuery({ ...payload, knowledge_base_id: kbId });
      setModalOpen(false);
      setEditing(null);
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.verifiedQuery.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const setStatus = async (row: VerifiedQuery, op: 'activate' | 'retire') => {
    setBusyId(row.id);
    try {
      if (op === 'activate') await activateVerifiedQuery(kbId, row.id, row.version);
      else await retireVerifiedQuery(kbId, row.id, row.version);
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.verifiedQuery.statusFailed'));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (row: VerifiedQuery) => {
    setBusyId(row.id);
    try {
      await deleteVerifiedQuery(kbId, row.id, row.version);
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.verifiedQuery.deleteFailed'));
    } finally {
      setBusyId(null);
    }
  };

  const runPreview = async () => {
    if (!previewDs || !previewQuestion.trim()) return;
    setPreviewing(true);
    try {
      setPreview(await previewVerifiedMatch(kbId, previewDs, previewQuestion.trim()));
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.verifiedQuery.previewFailed'));
    } finally {
      setPreviewing(false);
    }
  };

  const columns: ColumnsType<VerifiedQuery> = [
    { title: t('domainKnowledge.verifiedQuery.name'), dataIndex: 'name', key: 'name' },
    {
      title: t('domainKnowledge.verifiedQuery.dataSource'), dataIndex: 'data_source_id',
      key: 'ds', render: (id: string) => dsName(id),
    },
    {
      title: t('domainKnowledge.verifiedQuery.questions'), key: 'questions', width: 100,
      render: (_: unknown, row) => row.questions.length,
    },
    {
      title: t('domainKnowledge.verifiedQuery.status'), dataIndex: 'status', key: 'status',
      width: 110,
      render: (status: string) => (
        <Tag color={STATUS_COLOR[status] || 'default'}>
          {t(`domainKnowledge.verifiedQuery.status.${status}`)}
        </Tag>
      ),
    },
    { title: t('domainKnowledge.verifiedQuery.version'), dataIndex: 'version', key: 'version', width: 90 },
    {
      title: t('domainKnowledge.verifiedQuery.updatedAt'), dataIndex: 'updated_at', key: 'updated',
      width: 170, render: (v: string | null) => v || '—',
    },
  ];
  if (canWrite) {
    columns.push({
      title: t('domainKnowledge.lineage.actions'), key: 'actions', width: 260,
      render: (_: unknown, row) => (
        <Space size={0} wrap>
          <Button type="text" size="small" icon={<EditOutlined />}
                  onClick={() => { setEditing(row); setModalOpen(true); }}>
            {t('common.edit')}
          </Button>
          {row.status !== 'active' && (
            <Button type="text" size="small" icon={<PlayCircleOutlined />}
                    loading={busyId === row.id}
                    onClick={() => void setStatus(row, 'activate')}>
              {t('domainKnowledge.verifiedQuery.activate')}
            </Button>
          )}
          {row.status === 'active' && (
            <Button type="text" size="small" icon={<StopOutlined />}
                    loading={busyId === row.id}
                    onClick={() => void setStatus(row, 'retire')}>
              {t('domainKnowledge.verifiedQuery.retire')}
            </Button>
          )}
          <Popconfirm title={t('domainKnowledge.verifiedQuery.deleteConfirm')}
                      onConfirm={() => void remove(row)}
                      okText={t('common.confirm')} cancelText={t('common.cancel')}>
            <Button type="text" size="small" danger icon={<DeleteOutlined />}
                    loading={busyId === row.id}>
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    });
  }

  if (loading) {
    return <div className="yx-kb-tab-loading"><Spin /></div>;
  }
  if (error) {
    return <Alert type="error" showIcon message={error}
                  action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>} />;
  }

  return (
    <div>
      <Alert type="info" showIcon style={{ marginBottom: 12 }}
             message={t('domainKnowledge.verifiedQuery.intro')} />
      {canWrite && (
        <Button type="primary" icon={<PlusOutlined />} style={{ marginBottom: 12 }}
                onClick={() => { setEditing(null); setModalOpen(true); }}>
          {t('domainKnowledge.verifiedQuery.create')}
        </Button>
      )}
      <Table rowKey="id" size="small" columns={columns} dataSource={items}
             pagination={false} locale={{ emptyText: <Empty /> }} />

      <Card size="small" style={{ marginTop: 16 }}
            title={t('domainKnowledge.verifiedQuery.previewTitle')}>
        <Space wrap>
          <Select style={{ width: 220 }} placeholder={t('domainKnowledge.verifiedQuery.dataSource')}
                  value={previewDs || undefined}
                  onChange={setPreviewDs}
                  options={dbSources.map((d) => ({ value: d.id, label: d.name }))} />
          <Input style={{ width: 360 }} value={previewQuestion}
                 placeholder={t('domainKnowledge.verifiedQuery.previewQuestion')}
                 onChange={(e) => setPreviewQuestion(e.target.value)}
                 onPressEnter={() => void runPreview()} />
          <Button type="primary" loading={previewing} onClick={() => void runPreview()}>
            {t('domainKnowledge.verifiedQuery.preview')}
          </Button>
        </Space>
        {preview && (
          <div style={{ marginTop: 12 }}>
            {preview.matched ? (
              <>
                <Typography.Text>
                  {t('domainKnowledge.verifiedQuery.previewHit', { name: preview.name })}
                </Typography.Text>
                <pre className="yx-kb-vq-preview">{preview.sql}</pre>
              </>
            ) : (
              <Typography.Text type="secondary">
                {t('domainKnowledge.verifiedQuery.previewMiss')}
                {preview.reason ? `（${preview.reason}）` : ''}
              </Typography.Text>
            )}
          </div>
        )}
      </Card>

      <VerifiedQueryFormModal
        open={modalOpen}
        editing={editing}
        dataSources={dbSources}
        submitting={submitting}
        onSubmit={(p) => void submit(p)}
        onCancel={() => { setModalOpen(false); setEditing(null); }}
      />
    </div>
  );
}
