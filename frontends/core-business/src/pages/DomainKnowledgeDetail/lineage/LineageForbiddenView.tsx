/**
 * 「血缘 → 禁止同查」视图。
 *
 * 列出该数据源的表对：`proposed`（建议，不生效）/ `active`（生效）/ `disabled`（停用）。
 * 动作矩阵见 `forbiddenModel.actionsFor`；`canWrite=false` 时**一个写按钮都不渲染**。
 * 四态齐：loading / error（可重试）/ empty / success。
 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Empty, message, Popconfirm, Space, Spin, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import {
  createLineageForbiddenPair, deleteLineageForbiddenPair,
  listLineageForbiddenPairs, suggestLineageForbiddenPairs,
  updateLineageForbiddenPair, type LineageForbiddenPair,
} from '@/api/lineage';
import { actionsFor, STATUS_TAGS } from './forbiddenModel';
import LineageForbiddenModal, { type ForbiddenPairForm } from './LineageForbiddenModal';

interface Props {
  kbId: string;
  dsId: string;
  canWrite?: boolean;
}

export default function LineageForbiddenView({ kbId, dsId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<LineageForbiddenPair[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await listLineageForbiddenPairs(kbId, dsId);
      setItems(data.items);
    } catch (e: any) {
      setError(e?.message || t('domainKnowledge.lineage.forbidden.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, dsId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const setStatus = async (row: LineageForbiddenPair, status: string) => {
    setBusyId(row.id);
    try {
      await updateLineageForbiddenPair(kbId, row.id, { status });
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.lineage.forbidden.saveFailed'));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (row: LineageForbiddenPair) => {
    setBusyId(row.id);
    try {
      await deleteLineageForbiddenPair(kbId, row.id);
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.lineage.forbidden.deleteFailed'));
    } finally {
      setBusyId(null);
    }
  };

  const suggest = async () => {
    setLoading(true);
    try {
      const out = await suggestLineageForbiddenPairs(kbId, dsId);
      message.success(t('domainKnowledge.lineage.forbidden.suggestResult', {
        created: out.created, skipped: out.skipped,
      }));
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.lineage.forbidden.suggestFailed'));
      setLoading(false);
    }
  };

  const submit = async (v: ForbiddenPairForm) => {
    setSubmitting(true);
    try {
      await createLineageForbiddenPair(kbId, { ...v, data_source_id: dsId });
      setModalOpen(false);
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.lineage.forbidden.createFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const columns: ColumnsType<LineageForbiddenPair> = [
    { title: t('domainKnowledge.lineage.forbidden.tableA'), dataIndex: 'table_a', key: 'a' },
    { title: t('domainKnowledge.lineage.forbidden.tableB'), dataIndex: 'table_b', key: 'b' },
    {
      title: t('domainKnowledge.lineage.origin'), dataIndex: 'origin', key: 'origin', width: 130,
      render: (origin: string) => (
        <Tag>{t(origin === 'derived'
          ? 'domainKnowledge.lineage.forbidden.originDerived'
          : 'domainKnowledge.lineage.forbidden.originManual')}</Tag>
      ),
    },
    {
      title: t('domainKnowledge.lineage.forbidden.status'), dataIndex: 'status', key: 'status',
      width: 130,
      render: (status: string) => {
        const meta = STATUS_TAGS[status];
        return <Tag color={meta?.color ?? 'default'}>{meta ? t(meta.key) : status}</Tag>;
      },
    },
    {
      title: t('domainKnowledge.lineage.note'), dataIndex: 'note', key: 'note',
      render: (note?: string | null) => note || '—',
    },
  ];
  if (canWrite) {
    columns.push({
      title: t('domainKnowledge.lineage.actions'), key: 'actions', width: 220,
      render: (_: unknown, row) => (
        <Space size={0}>
          {actionsFor(row.status).map((action) => {
            if (action === 'delete') {
              return (
                <Popconfirm
                  key={action}
                  title={t('domainKnowledge.lineage.forbidden.deleteConfirm')}
                  onConfirm={() => void remove(row)}
                  okText={t('common.confirm')}
                  cancelText={t('common.cancel')}
                >
                  <Button type="text" size="small" danger loading={busyId === row.id}>
                    {t('common.delete')}
                  </Button>
                </Popconfirm>
              );
            }
            const status = action === 'adopt' || action === 'enable' ? 'active' : 'disabled';
            return (
              <Button key={action} type="text" size="small"
                      loading={busyId === row.id}
                      onClick={() => void setStatus(row, status)}>
                {t(`domainKnowledge.lineage.forbidden.${action}`)}
              </Button>
            );
          })}
        </Space>
      ),
    });
  }

  if (loading) {
    return <div style={{ padding: 24, textAlign: 'center' }}><Spin /></div>;
  }
  if (error) {
    return (
      <Alert type="error" showIcon
             message={t('domainKnowledge.lineage.forbidden.loadFailed')}
             description={error}
             action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>} />
    );
  }

  return (
    <div>
      <div className="yx-kb-flex-header" style={{ marginBottom: 12 }}>
        <p className="yx-kb-graph-desc" style={{ margin: 0 }}>
          {t('domainKnowledge.lineage.forbidden.hint')}
        </p>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()}>
            {t('common.refresh')}
          </Button>
          {canWrite && (
            <Button onClick={() => void suggest()}>
              {t('domainKnowledge.lineage.forbidden.suggest')}
            </Button>
          )}
          {canWrite && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
              {t('domainKnowledge.lineage.forbidden.add')}
            </Button>
          )}
        </Space>
      </div>

      {items.length === 0 ? (
        <Empty description={t('domainKnowledge.lineage.forbidden.empty')} />
      ) : (
        <Table<LineageForbiddenPair>
          rowKey="id"
          columns={columns}
          dataSource={items}
          pagination={{ pageSize: 20, hideOnSinglePage: true, showSizeChanger: false }}
          size="middle"
        />
      )}

      {canWrite && (
        <LineageForbiddenModal
          open={modalOpen}
          submitting={submitting}
          onCancel={() => setModalOpen(false)}
          onSubmit={(v) => void submit(v)}
        />
      )}
    </div>
  );
}
