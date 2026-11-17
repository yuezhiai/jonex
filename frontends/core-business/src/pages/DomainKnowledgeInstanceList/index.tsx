import React, { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Input, Table, Breadcrumb, message, Pagination } from 'antd';
import { SearchOutlined, BlockOutlined } from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import type { OntologyInstanceRow } from '@/types/domainKnowledge';
import { getOntologyInstances, getOntologyEntityTypes } from '@/api/domainKnowledge';

const PAGE_SIZE = 10;

export default function DomainKnowledgeInstanceList() {
  const { t } = useTranslation();
  const { id, entityType } = useParams<{ id: string; entityType: string }>();
  const navigate = useNavigate();
  const decodedType = decodeURIComponent(entityType || '');

  const [instances, setInstances] = useState<OntologyInstanceRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeNameMap, setTypeNameMap] = useState<Record<string, string>>({});

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

  const fetchData = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getOntologyInstances({
        kbId: id,
        entityType: decodedType || undefined,
        keyword: keyword || undefined,
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
  }, [id, decodedType, keyword, page]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const breadcrumbItems = [
    {
      title: (
        <a onClick={() => navigate('/domain-knowledge')} style={{ color: 'var(--yx-text-secondary)' }}>
          {t('navigation.domainKnowledge')}
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
    { title: <span style={{ color: 'var(--yx-brand-dark)', fontWeight: 500 }}>{t('common.instance', { name: decodedType })}</span> },
  ];

  return (
    <>
      <div style={{ marginBottom: 8 }}>
        <Breadcrumb items={breadcrumbItems} />
      </div>

      <PageHeader
        backTo={() => navigate(`/domain-knowledge/${id}`)}
        title={`${decodedType} - ${t('domainKnowledge.ontologyInstances')}`}
        extra={
          <span style={{ fontSize: 12, color: 'var(--yx-text-muted)' }}>
            <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--yx-accent)', marginRight: 6 }}>
              {total.toLocaleString()}
            </span>
            {t('domainKnowledge.instanceTotalCount')}
          </span>
        }
        style={{ marginBottom: 4 }}
      />
      <div style={{ fontSize: 13, color: 'var(--yx-text-muted)', marginBottom: 20 }}>{t('domainKnowledge.instanceListDesc')}</div>

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
              <BlockOutlined style={{ color: 'var(--yx-accent)' }} /> {t('domainKnowledge.instanceList')}
            </h3>
            <Input
              prefix={<SearchOutlined />}
              placeholder={t('domainKnowledge.instanceSearchPlaceholder')}
              style={{ width: 200 }}
              value={keyword}
              onChange={(e) => {
                setKeyword(e.target.value);
                setPage(1);
              }}
            />
          </div>

          {error && instances.length === 0 ? (
            <div style={{ textAlign: 'center', padding: 48, color: 'var(--yx-text-muted)' }}>{error}</div>
          ) : (
            <>
              <Table
                columns={[
                  {
                    title: t('compile.instanceName'),
                    dataIndex: 'name',
                    key: 'name',
                    width: 220,
                    render: (v: string) => <strong style={{ color: 'var(--yx-brand-dark)' }}>{v}</strong>,
                  },
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
                        {typeNameMap[v] || v}
                      </span>
                    ),
                  },
                  {
                    title: t('domainKnowledge.alias'),
                    dataIndex: 'aliases',
                    key: 'aliases',
                    width: 200,
                    render: (v: unknown) => {
                      const arr = Array.isArray(v) ? v : typeof v === 'string' ? [v] : [];
                      return arr.length > 0 ? arr.slice(0, 3).join('、') + (arr.length > 3 ? '...' : '') : '—';
                    },
                  },
                  {
                    title: t('common.description'),
                    dataIndex: 'description',
                    key: 'description',
                    render: (v: string) => <span style={{ color: 'var(--yx-text-secondary)', fontSize: 13 }}>{v || '—'}</span>,
                  },
                  {
                    title: t('domainKnowledge.attributeCount'),
                    key: 'attrCount',
                    width: 80,
                    render: (_: unknown, r: OntologyInstanceRow) =>
                      r.attributes ? Object.keys(r.attributes).length : 0,
                  },
                  {
                    title: t('domainKnowledge.sourceDocuments'),
                    key: 'docCount',
                    width: 80,
                    render: (_: unknown, r: OntologyInstanceRow) => r.doc_ids?.length ?? 0,
                  },
                ]}
                dataSource={instances}
                rowKey="name"
                pagination={false}
                size="middle"
                loading={loading}
                locale={{ emptyText: t('domainKnowledge.emptyInstances') }}
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
      </PageContent>
    </>
  );
}
