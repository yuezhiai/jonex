import { useState, useMemo, useEffect } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import { Table, Button, Space, Popconfirm, message, Input } from 'antd';
import { EditOutlined, DeleteOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { RelationInstanceSummary, RelationInstanceRow } from '@/types/domainKnowledge';
import { useTranslation } from 'react-i18next';
import EditRelationModal from './EditRelationModal';
import { getOntologyRelationInstances, createOntologyRelation, deleteOntologyRelation } from '@/api/domainKnowledge';
import debounce from 'lodash/debounce';

const PAGE_SIZE = 10;

interface RelationTabProps {
  kbId: string;
  docId?: string;
  data?: RelationInstanceSummary[] | null;
  title?: string;
}

export default function RelationTab({ kbId, docId, data, title: propTitle }: RelationTabProps) {
  const { canWrite } = useKbPermission(kbId);
  const { t } = useTranslation();
  const title = propTitle ?? t('compile.relationType');

  // ── instance list ──
  const [instances, setInstances] = useState<RelationInstanceRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [keyword, setKeyword] = useState('');

  const fetchList = useMemo(
    () =>
      debounce(async (p: number, kw: string, kId: string, dId?: string) => {
        setLoading(true);
        try {
          const res = await getOntologyRelationInstances({
            kbId: kId,
            keyword: kw || undefined,
            page: p,
            pageSize: PAGE_SIZE,
            docId: dId || undefined,
          });
          setInstances(res.items);
          setTotal(res.total);
        } catch {
          message.error(t('common.loadFailed'));
        } finally {
          setLoading(false);
        }
      }, 300),
    [],
  );

  // keyword 变化 → 重置到第 1 页（fetchList 自带 debounce）
  // 挂载 / keyword 变化 / kbId 变化时重置到第 1 页
  useEffect(() => {
    setPage(1);
    fetchList(1, keyword.trim(), kbId, docId);
  }, [keyword, kbId, fetchList]);

  // ── relation type display map & options for modal ──
  const relationTypeDisplayMap = useMemo(() => {
    const map: Record<string, string> = {};
    (data ?? []).forEach((item) => {
      map[item.name] = item.display_name || item.name;
    });
    return map;
  }, [data]);

  const relationTypeOptions = useMemo(
    () => (data ?? []).map((item) => ({ value: item.name, label: item.display_name || item.name })),
    [data],
  );

  // ── modal ──
  const [modalOpen, setModalOpen] = useState(false);
  const [isCreate, setIsCreate] = useState(false);
  const [editData, setEditData] = useState<{
    sourceType: string;
    sourceName: string;
    relationType: string;
    targetType: string;
    targetName: string;
    attributes?: Record<string, unknown> | null;
  } | null>(null);

  const openCreate = () => {
    setIsCreate(true);
    setEditData(null);
    setModalOpen(true);
  };
  const openEdit = (row: RelationInstanceRow) => {
    setIsCreate(false);
    setEditData({
      sourceType: row.source_type,
      sourceName: row.source,
      relationType: row.relation_type,
      targetType: row.target_type,
      targetName: row.target,
      attributes: row.attributes,
    });
    setModalOpen(true);
  };
  const closeModal = () => {
    setModalOpen(false);
    setEditData(null);
  };

  const handleCreate = async (form: {
    sourceType: string;
    sourceName: string;
    relationType: string;
    targetType: string;
    targetName: string;
    attributes?: Record<string, unknown>;
  }) => {
    try {
      await createOntologyRelation(
        kbId,
        form.sourceType,
        form.sourceName,
        form.relationType,
        form.targetType,
        form.targetName,
        form.attributes,
      );
      message.success(t('common.createSuccess'));
      closeModal();
      setPage(1);
      fetchList(1, keyword, kbId, docId);
    } catch (e: unknown) {
      const err = e instanceof Error ? e.message : '';
      message.error(`${t('common.createFailed')}${err ? `: ${err}` : ''}`);
    }
  };

  const handleEdit = async (form: {
    sourceType: string;
    sourceName: string;
    relationType: string;
    targetType: string;
    targetName: string;
  }) => {
    try {
      const old = editData;
      if (!old) return;

      const identityChanged =
        old.sourceType !== form.sourceType ||
        old.sourceName !== form.sourceName ||
        old.relationType !== form.relationType ||
        old.targetType !== form.targetType ||
        old.targetName !== form.targetName;

      if (!identityChanged) {
        message.info(t('common.noChanges'));
        closeModal();
        return;
      }

      // 标识符变化 → 删除旧关系 + 创建新关系
      await deleteOntologyRelation(
        kbId,
        old.sourceType,
        old.sourceName,
        old.relationType,
        old.targetType,
        old.targetName,
      );
      await createOntologyRelation(
        kbId,
        form.sourceType,
        form.sourceName,
        form.relationType,
        form.targetType,
        form.targetName,
      );
      message.success(t('common.saveSuccess'));
      closeModal();
      fetchList(page, keyword, kbId, docId);
    } catch (e: unknown) {
      const err = e instanceof Error ? e.message : '';
      message.error(`${t('common.saveFailed')}${err ? `: ${err}` : ''}`);
    }
  };

  const handleDelete = async (row: RelationInstanceRow) => {
    try {
      await deleteOntologyRelation(kbId, row.source_type, row.source, row.relation_type, row.target_type, row.target);
      message.success(t('common.deleteSuccess'));
      fetchList(page, keyword, kbId, docId);
    } catch (e: unknown) {
      const err = e instanceof Error ? e.message : '';
      message.error(`${t('common.deleteFailed')}${err ? `: ${err}` : ''}`);
    }
  };

  // ── columns ──
  const columns: ColumnsType<RelationInstanceRow> = [
    {
      title: t('compile.relation.sourceObject'),
      dataIndex: 'source',
      key: 'source',
      width: 140,
      render: (v: string, r: RelationInstanceRow) => (
        <span
          style={{
            padding: '2px 8px',
            borderRadius: 6,
            background: '#eff6ff',
            color: '#3b82f6',
            fontSize: 12,
            fontWeight: 500,
          }}
        >
          <span style={{ fontSize: 10, opacity: 0.6 }}>{r.source_type}</span>
          <br />
          {v}
        </span>
      ),
    },
    {
      title: t('compile.relation.name'),
      dataIndex: 'relation_type',
      key: 'relation_type',
      width: 140,
      render: (v: string) => <strong style={{ color: '#0b2b5c' }}>{relationTypeDisplayMap[v] || v}</strong>,
    },
    {
      title: t('compile.relation.targetObject'),
      dataIndex: 'target',
      key: 'target',
      width: 140,
      render: (v: string, r: RelationInstanceRow) => (
        <span
          style={{
            padding: '2px 8px',
            borderRadius: 6,
            background: '#ecfdf5',
            color: '#059669',
            fontSize: 12,
            fontWeight: 500,
          }}
        >
          <span style={{ fontSize: 10, opacity: 0.6 }}>{r.target_type}</span>
          <br />
          {v}
        </span>
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 140,
      align: 'center',
      render: (_: unknown, record: RelationInstanceRow) => (
        <Space size="small">
          <Button
            type="text"
            size="small"
            icon={<EditOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => openEdit(record)}
          >
            {t('common.edit')}
          </Button>
          <Popconfirm
            title={t('common.confirmDelete')}
            description={t('common.deleteRelationConfirm')}
            disabled={!canWrite}
            onConfirm={() => handleDelete(record)}
            okText={t('common.confirm')}
            cancelText={t('common.cancel')}
          >
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={!canWrite}
              title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            >
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '16px 24px',
          borderTop: '1px solid #f1f5f9',
        }}
      >
        <div style={{ fontSize: 14, fontWeight: 600, color: '#0b2b5c' }}>
          {title}
          <span style={{ fontSize: 13, color: '#94a3b8', fontWeight: 400, marginLeft: 8 }}>
            {t('compile.totalInstances', { count: total })}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Input
            prefix={<SearchOutlined />}
            placeholder={t('common.search')}
            style={{ width: 200 }}
            size="small"
            value={keyword}
            onChange={(e) => {
              setKeyword(e.target.value);
            }}
            allowClear
          />
          <Button
            type="primary"
            size="small"
            icon={<PlusOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={openCreate}
          >
            {t('compile.relation.createBtn')}
          </Button>
        </div>
      </div>

      <Table
        rowKey={(r) => `${r.source_type}|${r.source}|${r.relation_type}|${r.target_type}|${r.target}`}
        columns={columns}
        dataSource={instances}
        loading={loading}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          onChange: (p) => {
            setPage(p);
            fetchList(p, keyword, kbId, docId);
          },
          showSizeChanger: false,
          showTotal: (tTotal) => t('compile.totalInstances', { count: tTotal }),
        }}
        size="middle"
        style={{ padding: '0 24px 24px' }}
        scroll={{ y: 'calc(100vh - 480px)' }}
        locale={{ emptyText: <span style={{ color: '#94a3b8' }}>{t('compile.emptyRelationInstances')}</span> }}
      />

      <EditRelationModal
        open={modalOpen}
        mode={isCreate ? 'create' : 'edit'}
        kbId={kbId}
        relationTypeOptions={relationTypeOptions}
        initialData={editData}
        onSave={isCreate ? handleCreate : handleEdit}
        onCancel={closeModal}
      />
    </>
  );
}
