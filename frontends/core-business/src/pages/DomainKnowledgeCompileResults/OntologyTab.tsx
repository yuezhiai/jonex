import { useState, useMemo, useEffect } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import debounce from 'lodash/debounce';
import { Table, Button, Space, Popconfirm, message, Input } from 'antd';
import { EditOutlined, DeleteOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { OntologyInstanceSummary, OntologyInstanceRow } from '@/types/domainKnowledge';
import { useTranslation } from 'react-i18next';
import EditOntologyModal from './EditOntologyModal';
import {
  createOntologyInstance,
  updateOntologyInstance,
  deleteOntologyInstance,
  getOntologyInstances,
} from '@/api/domainKnowledge';

const PAGE_SIZE = 10;

interface OntologyTabProps {
  kbId: string;
  docId?: string;
  data: OntologyInstanceSummary[] | null;
  title?: string;
}

export default function OntologyTab({ kbId, docId, data, title: propTitle }: OntologyTabProps) {
  const { canWrite } = useKbPermission(kbId);
  const { t } = useTranslation();
  const title = propTitle ?? t('compile.ontologyInstance');

  // ── 实例列表 ──
  const [instances, setInstances] = useState<OntologyInstanceRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [keyword, setKeyword] = useState('');

  const fetchData = useMemo(
    () =>
      debounce(async (p: number, kw: string, kId: string, dId?: string) => {
        setLoading(true);
        try {
          const res = await getOntologyInstances({
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

  // 挂载 / keyword 变化 / kbId 变化时重置到第 1 页
  useEffect(() => {
    setPage(1);
    fetchData(1, keyword.trim(), kbId, docId);
  }, [keyword, kbId, fetchData]);

  // ── 实体类型显示映射及弹窗下拉框选项 ──
  const entityTypeDisplayMap = useMemo(() => {
    const map: Record<string, string> = {};
    (data ?? []).forEach((item) => {
      map[item.name] = item.display_name || item.name;
    });
    return map;
  }, [data]);

  const ontologyOptions = useMemo(() => {
    return (data ?? []).map((item) => ({
      value: item.description || item.display_name || item.name,
      label: item.description || item.display_name || item.name,
    }));
  }, [data]);

  // ── modal ──
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [isCreate, setIsCreate] = useState(false);
  const [editingRecord, setEditingRecord] = useState<{
    name: string;
    type: string;
    aliases?: string[];
    description?: string;
    attributes?: Record<string, unknown> | null;
  } | null>(null);

  const openCreateModal = () => {
    setIsCreate(true);
    setEditingRecord(null);
    setIsModalOpen(true);
  };

  const openEditModal = (row: OntologyInstanceRow) => {
    setIsCreate(false);
    setEditingRecord({
      name: row.name,
      type: row.type,
      aliases: row.aliases,
      description: row.description,
      attributes: row.attributes,
    });
    setIsModalOpen(true);
  };

  const closeEditModal = () => {
    setIsModalOpen(false);
    setEditingRecord(null);
  };

  const handleCreate = async (data: {
    name: string;
    type: string;
    aliases?: string[];
    description?: string;
    attributes?: Record<string, unknown>;
  }) => {
    try {
      await createOntologyInstance({
        knowledge_base_id: kbId,
        entity_type: data.type,
        name: data.name,
        aliases: data.aliases,
        description: data.description,
        attributes: data.attributes,
      });
      message.success(t('compile.instanceCreated'));
      closeEditModal();
      setPage(1);
      fetchData(1, keyword, kbId);
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : '';
      message.error(`${t('common.createFailed')}${errMsg ? `: ${errMsg}` : ''}`);
    }
  };

  const handleEdit = async (data: {
    name: string;
    type: string;
    aliases?: string[];
    description?: string;
    attributes?: Record<string, unknown>;
  }) => {
    try {
      const old = editingRecord;
      if (!old?.name || !old?.type) return;

      const identityChanged = data.type !== old.type;

      if (identityChanged) {
        // 实体类型变化 → 删除旧实例 + 创建新实例
        await deleteOntologyInstance(kbId, old.type, old.name);
        await createOntologyInstance({
          knowledge_base_id: kbId,
          entity_type: data.type,
          name: data.name,
          aliases: data.aliases,
          description: data.description,
          attributes: data.attributes,
        });
      } else {
        const updates: Record<string, unknown> = {};
        if (data.name !== old.name) updates.name = data.name;
        if (data.description !== old.description) updates.description = data.description;
        if (JSON.stringify(data.aliases) !== JSON.stringify(old.aliases)) updates.aliases = data.aliases;
        if (JSON.stringify(data.attributes) !== JSON.stringify(old.attributes)) updates.attributes = data.attributes;

        if (Object.keys(updates).length === 0) {
          message.info(t('common.noChanges'));
          closeEditModal();
          return;
        }

        await updateOntologyInstance(kbId, old.type, old.name, updates);
      }

      message.success(t('common.saveSuccess'));
      closeEditModal();
      fetchData(page, keyword, kbId, docId);
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : '';
      message.error(`${t('common.saveFailed')}${errMsg ? `: ${errMsg}` : ''}`);
    }
  };

  const handleDelete = async (row: OntologyInstanceRow) => {
    try {
      await deleteOntologyInstance(kbId, row.type, row.name);
      message.success(t('common.deleteSuccess'));
      fetchData(page, keyword, kbId, docId);
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : '';
      message.error(`${t('common.deleteFailed')}${errMsg ? `: ${errMsg}` : ''}`);
    }
  };

  // ── columns ──
  const columns: ColumnsType<OntologyInstanceRow> = [
    {
      title: t('domainKnowledge.entityType'),
      dataIndex: 'type',
      key: 'type',
      width: 120,
      render: (v: string) => (
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
          {entityTypeDisplayMap[v] || v}
        </span>
      ),
    },
    {
      title: t('compile.instanceName'),
      dataIndex: 'name',
      key: 'name',
      width: 220,
      render: (v: string) => <strong style={{ color: '#0b2b5c' }}>{v}</strong>,
    },
    {
      title: t('domainKnowledge.alias'),
      dataIndex: 'aliases',
      key: 'aliases',
      width: 200,
      render: (v: string[]) => (v?.length > 0 ? v.slice(0, 3).join('、') + (v.length > 3 ? '...' : '') : '—'),
    },
    {
      title: t('common.description'),
      dataIndex: 'description',
      key: 'description',
      width: 400,
      ellipsis: true,
      render: (v: string) => <span style={{ color: '#64748b', fontSize: 13 }}>{v || '—'}</span>,
    },
    {
      title: t('domainKnowledge.attributeCount'),
      key: 'attrCount',
      width: 80,
      render: (_: unknown, r: OntologyInstanceRow) => (r.attributes ? Object.keys(r.attributes).length : 0),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 160,
      align: 'center',
      render: (_: unknown, record: OntologyInstanceRow) => (
        <Space size="small">
          <Button
            type="text"
            size="small"
            icon={<EditOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => openEditModal(record)}
          >
            {t('common.edit')}
          </Button>
          <Popconfirm
            title={t('common.confirmDelete')}
            description={t('compile.deleteConfirmDesc')}
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
          <span
            style={{
              fontSize: 13,
              color: '#94a3b8',
              fontWeight: 400,
              marginLeft: 8,
            }}
          >
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
            onChange={(e) => setKeyword(e.target.value)}
            allowClear
          />
          <Button
            type="primary"
            size="small"
            icon={<PlusOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={openCreateModal}
          >
            {t('compile.createInstance')}
          </Button>
        </div>
      </div>

      <Table
        rowKey="name"
        columns={columns}
        dataSource={instances}
        loading={loading}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          onChange: (p) => {
            setPage(p);
            fetchData(p, keyword, kbId);
          },
          showSizeChanger: false,
          showTotal: (tTotal) => t('compile.totalInstances', { count: tTotal }),
        }}
        size="middle"
        style={{ padding: '0 24px 24px' }}
        scroll={{ y: 'calc(100vh - 480px)' }}
        locale={{
          emptyText: <span style={{ color: '#94a3b8' }}>{t('compile.emptyOntologyInstances')}</span>,
        }}
      />

      <EditOntologyModal
        open={isModalOpen}
        mode={isCreate ? 'create' : 'edit'}
        record={editingRecord}
        ontologyOptions={ontologyOptions}
        onSave={isCreate ? handleCreate : handleEdit}
        onCancel={closeEditModal}
      />
    </>
  );
}
