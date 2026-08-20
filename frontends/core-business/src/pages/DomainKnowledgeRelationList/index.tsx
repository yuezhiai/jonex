import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import { useTranslation } from 'react-i18next';
import { Input, Table, Breadcrumb, message, Button, Modal } from 'antd';
import {
  ArrowLeftOutlined,
  SearchOutlined,
  LinkOutlined,
  PlusOutlined,
  EditOutlined,
  DeleteOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
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
        <a onClick={() => navigate('/domain-knowledge')} style={{ color: '#64748b' }}>
          {t('domainKnowledge.management')}
        </a>
      ),
    },
    {
      title: (
        <a onClick={() => navigate(`/domain-knowledge/${id}`)} style={{ color: '#64748b' }}>
          {t('domainKnowledge.detail')}
        </a>
      ),
    },
    { title: <span style={{ color: '#0b2b5c', fontWeight: 500 }}>{t('common.instance', { name: decodedName })}</span> },
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
              background: '#eff6ff',
              color: '#3b82f6',
              fontSize: 12,
              fontWeight: 500,
            }}
          >
            {v}
          </span>
          {r.source_type && (
            <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
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
            background: '#eff6ff',
            color: '#3b82f6',
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
              background: '#ecfdf5',
              color: '#059669',
              fontSize: 12,
              fontWeight: 500,
            }}
          >
            {v}
          </span>
          {r.target_type && (
            <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
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
    <div>
      <div style={{ marginBottom: 16 }}>
        <Breadcrumb items={breadcrumbItems} />
      </div>

      <a
        onClick={() => navigate(`/domain-knowledge/${id}`)}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          fontSize: 14,
          color: '#64748b',
          cursor: 'pointer',
          padding: '4px 0',
        }}
      >
        <ArrowLeftOutlined style={{ fontSize: 12 }} /> {t('domainKnowledge.backToResults')}
      </a>

      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          marginTop: 20,
          marginBottom: 24,
          padding: '20px 24px',
          background: '#fff',
          borderRadius: 14,
          border: '1px solid #eef2f6',
        }}
      >
        <div
          style={{
            width: 52,
            height: 52,
            borderRadius: 12,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 24,
            background: '#f0f9ff',
            color: '#8b5cf6',
            flexShrink: 0,
          }}
        >
          <LinkOutlined />
        </div>
        <div style={{ flex: 1 }}>
          <h2 style={{ fontSize: 20, fontWeight: 700, color: '#0b2b5c', margin: 0 }}>
            {t('domainKnowledge.instanceTitle', { name: decodedName })}
          </h2>
          <div style={{ fontSize: 13, color: '#94a3b8', marginTop: 4 }}>{t('domainKnowledge.instanceDesc')}</div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 24, fontWeight: 700, color: '#8b5cf6' }}>{total.toLocaleString()}</div>
          <div style={{ fontSize: 12, color: '#94a3b8' }}>{t('domainKnowledge.totalRelations')}</div>
        </div>
      </div>

      <div
        style={{
          background: '#fff',
          borderRadius: 14,
          border: '1px solid #eef2f6',
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
              color: '#0b2b5c',
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
          <div style={{ textAlign: 'center', padding: 48, color: '#94a3b8' }}>{error}</div>
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
              locale={{ emptyText: <span style={{ color: '#94a3b8' }}>{t('compile.emptyRelationInstances')}</span> }}
            />

            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'flex-end',
                gap: 6,
                padding: '12px 0 0',
                borderTop: '1px solid #eef2f6',
                marginTop: 12,
              }}
            >
              <span
                className={`yx-page-btn${page <= 1 ? ' disabled' : ''}`}
                onClick={() => page > 1 && setPage((p) => p - 1)}
                style={{
                  width: 34,
                  height: 34,
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  borderRadius: 8,
                  border: '1px solid #e2e8f0',
                  cursor: page <= 1 ? 'not-allowed' : 'pointer',
                  color: '#94a3b8',
                  fontSize: 12,
                  opacity: page <= 1 ? 0.4 : 1,
                }}
              >
                {'<'}
              </span>
              {Array.from({ length: Math.min(totalPages, 5) }, (_, i) => {
                const n = i + 1;
                return (
                  <span
                    key={n}
                    className={`yx-page-btn${n === page ? ' active' : ''}`}
                    onClick={() => setPage(n)}
                    style={{
                      width: 34,
                      height: 34,
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      borderRadius: 8,
                      background: n === page ? '#3b82f6' : 'transparent',
                      color: n === page ? '#fff' : '#64748b',
                      fontWeight: n === page ? 600 : 400,
                      fontSize: 13,
                      cursor: 'pointer',
                      border: n === page ? 'none' : '1px solid #e2e8f0',
                    }}
                  >
                    {n}
                  </span>
                );
              })}
              <span
                className={`yx-page-btn${page >= totalPages ? ' disabled' : ''}`}
                onClick={() => page < totalPages && setPage((p) => p + 1)}
                style={{
                  width: 34,
                  height: 34,
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  borderRadius: 8,
                  border: '1px solid #e2e8f0',
                  cursor: page >= totalPages ? 'not-allowed' : 'pointer',
                  color: '#94a3b8',
                  fontSize: 12,
                  opacity: page >= totalPages ? 0.4 : 1,
                }}
              >
                {'>'}
              </span>
              <span style={{ fontSize: 13, color: '#94a3b8', marginLeft: 12 }}>
                {t('domainKnowledge.pagination', { total: total.toLocaleString(), page, pages: totalPages })}
              </span>
            </div>
          </>
        )}
      </div>

      <div style={{ marginTop: 12 }}>
        <a
          onClick={() => navigate(`/domain-knowledge/${id}`)}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 8,
            padding: '8px 20px',
            borderRadius: 8,
            border: '1px solid #d1d5db',
            background: '#fff',
            color: '#64748b',
            cursor: 'pointer',
            fontSize: 13,
            textDecoration: 'none',
          }}
        >
          <ArrowLeftOutlined /> {t('domainKnowledge.backToResults')}
        </a>
      </div>

      <RelationFormModal
        ref={modalRef}
        id={id || ''}
        typeNameMap={typeNameMap}
        relTypeOptions={relTypeOptions}
        onSaved={handleSaved}
      />
    </div>
  );
}
