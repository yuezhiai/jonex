import { useState, useMemo, useEffect } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import debounce from 'lodash/debounce';
import { Table, Button, Space, Popconfirm, message, Input, Modal } from 'antd';
import { EditOutlined, DeleteOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { OntologyInstanceSummary, OntologyInstanceRow } from '@/types/domainKnowledge';
import { useTranslation } from 'react-i18next';
import EditOntologyModal from './EditOntologyModal';
import useFillHeight from '@/hooks/useFillHeight';
import {
  createOntologyInstance,
  updateOntologyInstance,
  deleteOntologyInstance,
  getOntologyInstances,
  getOntologyEntityTypes,
} from '@/api/domainKnowledge';
import './index.scss';

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
  // Neo4j 不可用时后端把 items/total 降级为空与 0：标题里的计数改显示「—」
  const [degraded, setDegraded] = useState(false);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [keyword, setKeyword] = useState('');
  // 表体高度按真实容器量，而不是按视口猜（见 useFillHeight）
  const [tableHostRef, tableBodyHeight] = useFillHeight();

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
          setDegraded(res.degraded === true);
        } catch (err: any) {
          message.error(err?.message || t('common.loadFailed'));
        } finally {
          setLoading(false);
        }
      }, 300),
    [],
  );

  // 挂载 / keyword / kbId / docId 变化时重置到第 1 页（docId 过滤依赖，必须纳入）
  useEffect(() => {
    setPage(1);
    fetchData(1, keyword.trim(), kbId, docId);
  }, [keyword, kbId, docId, fetchData]);

  // ── 实体类型显示映射及弹窗下拉框选项 ──
  // 实体类型是 KB 级概念：文档页拿到的 data 被裁过（只留本文档已有的类型，本文档没有实体时是空数组，
  // 建实例弹窗就会无类型可选），所以文档上下文里补一次 KB 全量，作为下拉/映射的来源。
  const [kbWideTypes, setKbWideTypes] = useState<OntologyInstanceSummary[] | null>(null);
  useEffect(() => {
    if (!docId) return;
    let alive = true;
    getOntologyEntityTypes(kbId)
      .then((res) => {
        if (alive) setKbWideTypes(res.items);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [kbId, docId]);

  // 文档页：KB 全量（加载中先退回文档级列表）；KB 级资产页：data 本身就是全量
  const typeSource = useMemo(() => (docId ? (kbWideTypes ?? data ?? []) : (data ?? [])), [docId, kbWideTypes, data]);

  const entityTypeDisplayMap = useMemo(() => {
    const map: Record<string, string> = {};
    (typeSource ?? []).forEach((item) => {
      map[item.name] = item.display_name || item.name;
    });
    return map;
  }, [typeSource]);

  const ontologyOptions = useMemo(() => {
    return (typeSource ?? []).map((item) => ({
      // 提交给后端的必须是类型标识（item.name），显示用人类可读文案
      value: item.name,
      label: item.description || item.display_name || item.name,
    }));
  }, [typeSource]);

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
        // 文档结果页（带 docId）建的实例归属该文档；KB 级「编译资产」页不带 → 全库实例
        ...(docId ? { document_id: docId } : {}),
      });
      message.success(t('compile.instanceCreated'));
      closeEditModal();
      setPage(1);
      fetchData(1, keyword, kbId, docId);
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
    const old = editingRecord;
    if (!old?.name || !old?.type) return;

    // 类型/名称变更与其余字段一样走一次 PUT：后端原地迁移定位键，
    // 目标键已存在时改类型自动合并，改名则回 1003 要求前端先确认。
    const updates: Record<string, unknown> = {};
    if (data.type !== old.type) updates.entity_type = data.type;
    if (data.name !== old.name) updates.name = data.name;
    if (data.description !== old.description) updates.description = data.description;
    if (JSON.stringify(data.aliases) !== JSON.stringify(old.aliases)) updates.aliases = data.aliases;
    if (JSON.stringify(data.attributes) !== JSON.stringify(old.attributes)) updates.attributes = data.attributes;

    if (Object.keys(updates).length === 0) {
      message.info(t('common.noChanges'));
      closeEditModal();
      return;
    }

    const submit = async (mergeTarget?: boolean) => {
      const res = await updateOntologyInstance(kbId, old.type, old.name, updates, mergeTarget);
      message.success(res?.merged ? t('compile.instanceMerged') : t('common.saveSuccess'));
      closeEditModal();
      fetchData(page, keyword, kbId, docId);
    };

    try {
      await submit();
      return;
    } catch (e: unknown) {
      const bizCode = (e as { bizCode?: number }).bizCode;
      // 1003：改名撞上已存在的同名实例。合并不可逆，必须由用户显式确认后再重发。
      if (bizCode === 1003 && typeof updates.name === 'string') {
        Modal.confirm({
          title: t('compile.instanceMergeConfirmTitle'),
          content: t('compile.instanceMergeConfirmBody', {
            name: updates.name,
            from: old.name,
          }),
          okText: t('compile.instanceMergeConfirmOk'),
          cancelText: t('compile.instanceMergeConfirmCancel'),
          onOk: async () => {
            try {
              await submit(true);
            } catch (err: unknown) {
              const errMsg = err instanceof Error ? err.message : '';
              message.error(`${t('common.saveFailed')}${errMsg ? `: ${errMsg}` : ''}`);
            }
          },
        });
        return;
      }
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
            background: 'var(--yx-info-bg)',
            color: 'var(--yx-accent)',
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
      render: (v: string) => <strong style={{ color: 'var(--yx-brand-dark)' }}>{v}</strong>,
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
      render: (v: string) => <span style={{ color: 'var(--yx-text-secondary)', fontSize: 13 }}>{v || '—'}</span>,
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
    <div className="yx-fill-view">
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: 16,
          flexShrink: 0,
        }}
      >
        <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>
          {title}
          <span
            style={{
              fontSize: 13,
              color: 'var(--yx-text-muted)',
              fontWeight: 400,
              marginLeft: 8,
            }}
          >
            {degraded ? '—' : t('compile.totalInstances', { count: total })}
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

      <div className="yx-table-fill" ref={tableHostRef}>
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
              fetchData(p, keyword, kbId, docId);
            },
            showSizeChanger: false,
            showTotal: (tTotal) => t('compile.totalInstances', { count: tTotal }),
          }}
          size="middle"
          style={{ padding: '0 16px' }}
          scroll={{ y: tableBodyHeight }}
          locale={{
            emptyText: t('compile.emptyOntologyInstances'),
          }}
        />
      </div>

      <EditOntologyModal
        open={isModalOpen}
        mode={isCreate ? 'create' : 'edit'}
        record={editingRecord}
        ontologyOptions={ontologyOptions}
        onSave={isCreate ? handleCreate : handleEdit}
        onCancel={closeEditModal}
      />
    </div>
  );
}
