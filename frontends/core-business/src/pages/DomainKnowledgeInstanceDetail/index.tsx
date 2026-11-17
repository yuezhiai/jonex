import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, Table, Tag, Descriptions, Breadcrumb, Button } from 'antd';
import { LinkOutlined, FileTextOutlined } from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { MOCK_GRAPH_INSTANCES } from '@/data/mock';
import type { GraphInstance } from '@/data/mock';

export default function DomainKnowledgeInstanceDetail() {
  const { t } = useTranslation();
  const { id, instanceId } = useParams<{ id: string; instanceId: string }>();
  const navigate = useNavigate();

  const instance: GraphInstance | undefined = useMemo(
    () => MOCK_GRAPH_INSTANCES.find((inst) => inst.id === instanceId),
    [instanceId],
  );

  if (!instance) {
    return (
      <>
        <PageHeader
          backTo={() => navigate(`/domain-knowledge/${id}/graph`)}
          title={t('route.ontologyInstanceDetail')}
        />
        <PageContent showCard={false}>
          <Card
            className="yx-card"
            style={{ borderRadius: 10, border: '1px solid var(--yx-border-light)', boxShadow: '0 1px 4px rgba(0,0,0,0.04)' }}
            styles={{ body: { padding: 48, textAlign: 'center' as const } }}
          >
            <div style={{ fontSize: 16, color: 'var(--yx-text-muted)' }}>{t('domainKnowledge.entityNotFound')}</div>
            <Button type="primary" style={{ marginTop: 16 }} onClick={() => navigate(`/domain-knowledge/${id}/graph`)}>
              {t('domainKnowledge.backToGraph')}
            </Button>
          </Card>
        </PageContent>
      </>
    );
  }

  const propertyColumns = [
    {
      title: t('domainKnowledge.propertyName'),
      dataIndex: 'key',
      key: 'key',
      width: 160,
      render: (v: string) => <span style={{ fontWeight: 600, color: 'var(--yx-brand-dark)', fontSize: 13 }}>{v}</span>,
    },
    {
      title: t('domainKnowledge.propertyValue'),
      dataIndex: 'value',
      key: 'value',
      render: (v: string) => <span style={{ color: '#475569', fontSize: 13 }}>{v}</span>,
    },
  ];

  const relationColumns = [
    {
      title: t('compile.relation.name'),
      dataIndex: 'name',
      key: 'name',
      width: 160,
      render: (v: string) => (
        <Tag color="processing" style={{ fontSize: 12 }}>
          {v}
        </Tag>
      ),
    },
    {
      title: t('domainKnowledge.relatedEntity'),
      dataIndex: 'target',
      key: 'target',
      render: (v: string) => <span style={{ color: '#475569', fontSize: 13 }}>{v}</span>,
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 100,
      render: (_: unknown, record: { id: string }) => (
        <a className="yx-table-action" onClick={() => navigate(`/domain-knowledge/${id}/graph/relations/${record.id}`)}>
          {t('domainKnowledge.viewDetails')}
        </a>
      ),
    },
  ];

  return (
    <>
      {/* Breadcrumb */}
      <div style={{ marginBottom: 8 }}>
        <Breadcrumb
          items={[
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
            {
              title: (
                <a onClick={() => navigate(`/domain-knowledge/${id}/graph`)} style={{ color: 'var(--yx-text-secondary)' }}>
                  {t('domainKnowledge.graphBreadcrumb')}
                </a>
              ),
            },
            { title: <span style={{ color: 'var(--yx-brand-dark)', fontWeight: 500 }}>{t('route.ontologyInstanceDetail')}</span> },
          ]}
        />
      </div>

      <PageHeader
        backTo={() => navigate(`/domain-knowledge/${id}/graph`)}
        title={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
            {instance.name}
            <Tag color="blue" style={{ fontSize: 12 }}>
              {instance.type}
            </Tag>
          </span>
        }
      />

      <PageContent showCard={false}>
        {/* Entity Info */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
          {/* Properties */}
          <Card
            className="yx-card"
            style={{ borderRadius: 10, border: '1px solid var(--yx-border-light)', boxShadow: '0 1px 4px rgba(0,0,0,0.04)' }}
            styles={{ body: { padding: 24 } }}
            title={
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <FileTextOutlined style={{ color: 'var(--yx-accent)' }} />
                <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>
                  {t('domainKnowledge.entityProperties')}
                </span>
              </div>
            }
          >
            <Table
              columns={propertyColumns}
              dataSource={instance.properties}
              rowKey="key"
              pagination={false}
              size="middle"
              showHeader={false}
            />
          </Card>

          {/* Relations */}
          <Card
            className="yx-card"
            style={{ borderRadius: 10, border: '1px solid var(--yx-border-light)', boxShadow: '0 1px 4px rgba(0,0,0,0.04)' }}
            styles={{ body: { padding: 24 } }}
            title={
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <LinkOutlined style={{ color: '#10b981' }} />
                <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>
                  {t('domainKnowledge.relatedRelations')}
                </span>
              </div>
            }
          >
            <Table
              columns={relationColumns}
              dataSource={instance.relations}
              rowKey="id"
              pagination={false}
              size="middle"
            />
          </Card>
        </div>

        {/* Source Documents */}
        <Card
          className="yx-card"
          style={{
            borderRadius: 10,
            border: '1px solid var(--yx-border-light)',
            boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
            marginTop: 20,
          }}
          styles={{ body: { padding: 24 } }}
          title={
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <FileTextOutlined style={{ color: '#f97316' }} />
              <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>
                {t('domainKnowledge.sourceDocuments')}
              </span>
            </div>
          }
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {instance.sourceDocs.map((doc, idx) => (
              <div
                key={idx}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  padding: '10px 14px',
                  borderRadius: 8,
                  background: 'var(--yx-bg-subtle)',
                  border: '1px solid var(--yx-border-light)',
                  fontSize: 13,
                  color: '#475569',
                }}
              >
                <FileTextOutlined style={{ color: 'var(--yx-text-muted)' }} />
                {doc}
              </div>
            ))}
          </div>
        </Card>
      </PageContent>
    </>
  );
}
