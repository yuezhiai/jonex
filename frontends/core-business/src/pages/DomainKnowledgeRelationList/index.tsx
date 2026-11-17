import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import { useTranslation } from 'react-i18next';
import { Input, Table, Breadcrumb, message, Button, Modal, Pagination } from 'antd';
import {
  SearchOutlined,
  LinkOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import type { RelationInstanceRow } from '@/types/domainKnowledge';
import {
  getOntologyRelationInstances,
  getOntologyEntityTypes,
  getOntologyRelationTypes,
  deleteOntologyRelation,
} from '@/api/domainKnowledge';
import RelationFormModal from './RelationFormModal';
import type { RelationFormModalHandle } from './RelationFormModal';

const PAGE_SIZE = 10;

export default function DomainKnowledgeRelationList() {
  const { t } = useTranslation();
  const { id, relationName } = useParams<{ id: string; relationName: string }>();
  const { canWrite } = useKbPermission(id);
  const navigate = useNavigate();
  const decodedName = decodeURIComponent(relationName || '');

  const [instances, setInstances] = useState<RelationInstanceRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeNameMap, setTypeNameMap] = useState<Record<string, string>>({});
  const [relTypeNameMap, setRelTypeNameMap] = useState<Record<string, string>>({});
  const [relTypeOptions, setRelTypeOptions] = useState<Array<{ value: string; label: string }>>([]);
  const modalRef = useRef<RelationFormModalHandle>(null);

  // 加载实体类型中文名映射
  useEffect(() => {
    if (!id) return;
    getOntologyEntityTypes(id)
      .then((res) => {
        const map: Record<string, string> = {};
        res.items.forEach((et) => {
          map[et.name] = et.display_name || et.name;
        });
        setTypeNameMap(map);
      })
      .catch(() => {});
  }, [id]);

  // 加载关系类型中文名映射 + 选项
  useEffect(() => {
    if (!id) return;
    getOntologyRelationTypes(id)
      .then((res) => {
        const map: Record<string, string> = {};
        const opts: Array<{ value: string; label: string }> = [];
        res.items.forEach((rt) => {
          map[rt.name] = rt.display_name || rt.name;
          opts.push({ value: rt.name, label: rt.display_name || rt.name });
        });
        setRelTypeNameMap(map);
        setRelTypeOptions(opts);
      })
      .catch(() => {});
  }, [id]);

  const fetchData = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getOntologyRelationInstances({
        kbId: id,
        relationType: decodedName || undefined,
        sourceName: keyword || undefined,
        targetName: keyword || undefined,
        docId: undefined,
        sourceType: undefined,
        targetType: undefined,
        page,
        pageSize: PAGE_SIZE,
      });
      setInstances(result.items);
      setTotal(result.total);
    } catch (err: any) {
      setError(err?.message || t('common.loadFailed'));
      message.error(err?.message || t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [id, decodedName, keyword, page, t]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // 保存成功后回调
  const handleSaved = useCallback(() => {
    message.success(t('common.saveSuccess'));
    setPage(1);
    fetchData();
  }, [t, fetchData]);

  // ── 删除确认 ──
  const handleDelete = useCallback(
    (row: RelationInstanceRow) => {
      if (!id) return;
      const name = `${row.source} → ${row.target} (${row.relation_type})`;
      Modal.confirm({
        title: t('common.confirmDelete'),
        icon: <ExclamationCircleOutlined />,
        content: t('common.confirmDeleteMessage', { name }),
        okText: t('common.confirmDelete'),
        cancelText: t('common.cancel'),
        okButtonProps: { danger: true },
        centered: true,
        onOk: async () => {
          try {
            await deleteOntologyRelation(
              id,
              row.source_type,
              row.source,
              row.relation_type,
              row.target_type,
              row.target,
            );
            message.success(t('common.deleteSuccess'));
            fetchData();
          } catch (err: any) {
            message.error(err?.message || t('common.deleteFailed'));
          }
        },
      });
    },
    [id, t, fetchData],
  );

  // ── 面包屑 ──
  const breadcrumbItems = [
    {
      title: (
        <a onClick={() => navigate('/domain-knowledge')} style={{ color: 'var(--yx-text-secondary)' }}>
          {t('domainKnowledge.management')}
        </a>
      ),
    },
    {
      title: (
        <a onClick={() => navigate(`/domain-knowledge/${id}`)} style={{ color: 'var(--yx-text-secondary)' }}>
          {t('domainKnowledge.detail')}
        </a>
      ),
    },
    { title: <span style={{ color: 'var(--yx-brand-dark)', fontWeight: 500 }}>{t('common.instance', { name: decodedName })}</span> },
  ];

  // ── 表格列 ──
  const columns = [
    {
      title: t('compile.relation.sourceObject'),
      dataIndex: 'source',
      key: 'source',
      width: 200,
      render: (v: string, r: RelationInstanceRow) => (
        <div>
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
            {v}
          </span>
          {r.source_type && (
            <div style={{ fontSize: 11, color: 'var(--yx-text-secondary)', marginTop: 2 }}>
              {typeNameMap[r.source_type] || r.source_type}
            </div>
          )}
        </div>
      ),
    },
    {
      title: t('compile.relation.type'),
      dataIndex: 'relation_type',
      key: 'relation_type',
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
          {relTypeNameMap[v] || v}
        </span>
      ),
    },
    {
      title: t('compile.relation.targetObject'),
      dataIndex: 'target',
      key: 'target',
      width: 200,
      render: (v: string, r: RelationInstanceRow) => (
        <div>
          <span
            style={{
              padding: '2px 8px',
              borderRadius: 6,
              background: 'var(--yx-success-bg)',
              color: '#059669',
              fontSize: 12,
              fontWeight: 500,
            }}
          >
            {v}
          </span>
          {r.target_type && (
            <div style={{ fontSize: 11, color: 'var(--yx-text-secondary)', marginTop: 2 }}>
              {typeNameMap[r.target_type] || r.target_type}
            </div>
          )}
        </div>
      ),
    },
    {
      title: t('compile.confidence'),
      dataIndex: 'confidence',
      key: 'confidence',
      width: 80,
      render: (v: number | null) => (v != null ? `${(v * 100).toFixed(0)}%` : '—'),
    },
    {
      title: t('common.operation'),
      key: 'action',
      width: 120,
      fixed: 'right' as const,
      render: (_: unknown, row: RelationInstanceRow) => (
        <div style={{ display: 'flex', gap: 8 }}>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => modalRef.current?.openEdit(row)}
          >
            {t('common.edit')}
          </Button>
          <Button
            type="link"
            size="small"
            danger
            icon={<DeleteOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => handleDelete(row)}
          >
            {t('common.delete')}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <>
      <div style={{ marginBottom: 8 }}>
        <Breadcrumb items={breadcrumbItems} />
      </div>

      <PageHeader
        backTo={() => navigate(`/domain-knowledge/${id}`)}
        title={t('domainKnowledge.instanceTitle', { name: decodedName })}
        extra={
          <span style={{ fontSize: 12, color: 'var(--yx-text-muted)' }}>
            <span style={{ fontSize: 20, fontWeight: 700, color: '#8b5cf6', marginRight: 6 }}>
              {total.toLocaleString()}
            </span>
            {t('domainKnowledge.totalRelations')}
          </span>
        }
        style={{ marginBottom: 4 }}
      />
      <div style={{ fontSize: 13, color: 'var(--yx-text-muted)', marginBottom: 20 }}>{t('domainKnowledge.instanceDesc')}</div>

      <PageContent showCard={false}>
        <div
          style={{
            background: '#fff',
            borderRadius: 10,
            border: '1px solid var(--yx-border-light)',
            padding: 24,
            boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
            <h3
              style={{
                margin: 0,
                fontSize: 15,
                fontWeight: 600,
                color: 'var(--yx-brand-dark)',
                display: 'flex',
                alignItems: 'center',
                gap: 8,
              }}
            >
              <LinkOutlined style={{ color: '#8b5cf6' }} /> {t('domainKnowledge.relationList')}
            </h3>
            <div style={{ display: 'flex', gap: 12 }}>
              <Input
                prefix={<SearchOutlined />}
                placeholder={t('domainKnowledge.sourceTargetPlaceholder')}
                style={{ width: 200 }}
                value={keyword}
                onChange={(e) => {
                  setKeyword(e.target.value);
                  setPage(1);
                }}
              />
              <Button
                type="primary"
                icon={<PlusOutlined />}
                disabled={!canWrite}
                title={canWrite ? undefined : t('domainSpace.noManagePermission')}
                onClick={() => modalRef.current?.openCreate()}
              >
                {t('domainKnowledge.newRelation')}
              </Button>
            </div>
          </div>

          {error && instances.length === 0 ? (
            <div style={{ textAlign: 'center', padding: 48, color: 'var(--yx-text-muted)' }}>{error}</div>
          ) : (
            <>
              <Table
                columns={columns}
                dataSource={instances}
                rowKey={(r, i) => `${r.source}-${r.target}-${r.relation_type}-${i}`}
                pagination={false}
                size="middle"
                loading={loading}
                scroll={{ x: 720 }}
                locale={{ emptyText: t('compile.emptyRelationInstances') }}
              />

              <Pagination
                current={page}
                pageSize={PAGE_SIZE}
                total={total}
                showSizeChanger={false}
                showTotal={(count) =>
                  t('domainKnowledge.pagination', { total: count.toLocaleString(), page, pages: totalPages })
                }
                onChange={setPage}
                style={{ marginTop: 12, display: 'flex', justifyContent: 'flex-end' }}
              />
            </>
          )}
        </div>

        <RelationFormModal
          ref={modalRef}
          id={id || ''}
          typeNameMap={typeNameMap}
          relTypeOptions={relTypeOptions}
          onSaved={handleSaved}
        />
      </PageContent>
    </>
  );
}
