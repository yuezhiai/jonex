import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, Table, Tag, Breadcrumb, Button } from 'antd';
import { ArrowLeftOutlined, LinkOutlined, FileTextOutlined, NodeIndexOutlined } from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { MOCK_GRAPH_RELATIONS } from '@/data/mock';
import type { GraphRelation } from '@/data/mock';

export default function DomainKnowledgeRelationDetail() {
  const { t } = useTranslation();
  const { id, relationId } = useParams<{ id: string; relationId: string }>();
  const navigate = useNavigate();

  const relation: GraphRelation | undefined = useMemo(
    () => MOCK_GRAPH_RELATIONS.find((rel) => rel.id === relationId),
    [relationId],
  );

  if (!relation) {
    return (
      <>
        <PageHeader backTo={() => navigate(`/domain-knowledge/${id}/graph`)} title={t('route.relationDetail')} />
        <PageContent showCard={false}>
          <Card
            className="yx-card"
            style={{ borderRadius: 10, border: '1px solid var(--yx-border-light)', boxShadow: '0 1px 4px rgba(0,0,0,0.04)' }}
            styles={{ body: { padding: 48, textAlign: 'center' as const } }}
          >
            <div style={{ fontSize: 16, color: 'var(--yx-text-muted)' }}>{t('domainKnowledge.relationNotFound')}</div>
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
            { title: <span style={{ color: 'var(--yx-brand-dark)', fontWeight: 500 }}>{t('route.relationDetail')}</span> },
          ]}
        />
      </div>

      <PageHeader
        backTo={() => navigate(`/domain-knowledge/${id}/graph`)}
        title={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
            {relation.name}
            <Tag color="green" style={{ fontSize: 12 }}>
              {relation.type}
            </Tag>
          </span>
        }
      />

      <PageContent showCard={false}>
        {/* Relation Overview */}
        <Card
          className="yx-card"
          style={{
            borderRadius: 10,
            border: '1px solid var(--yx-border-light)',
            boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
            marginBottom: 20,
          }}
          styles={{ body: { padding: 24 } }}
          title={
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <LinkOutlined style={{ color: '#10b981' }} />
              <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>
                {t('domainKnowledge.relationOverview')}
              </span>
            </div>
          }
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '10px 18px',
                borderRadius: 10,
                background: 'var(--yx-info-bg)',
                border: '1px solid #dbeafe',
              }}
            >
              <NodeIndexOutlined style={{ color: 'var(--yx-accent)', fontSize: 16 }} />
              <div>
                <div style={{ fontSize: 11, color: 'var(--yx-text-muted)' }}>{t('domainKnowledge.sourceEntity')}</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>{relation.sourceEntity}</div>
              </div>
            </div>

            <div style={{ fontSize: 20, color: 'var(--yx-text-muted)', fontWeight: 700 }}>
              <ArrowLeftOutlined style={{ transform: 'rotate(180deg)' }} />
            </div>

            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '10px 18px',
                borderRadius: 10,
                background: 'var(--yx-success-bg)',
                border: '1px solid #bbf7d0',
              }}
            >
              <NodeIndexOutlined style={{ color: '#10b981', fontSize: 16 }} />
              <div>
                <div style={{ fontSize: 11, color: 'var(--yx-text-muted)' }}>{t('domainKnowledge.targetEntity')}</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>{relation.targetEntity}</div>
              </div>
            </div>
          </div>
        </Card>

        {/* Properties and Source Snippets */}
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
                  {t('domainKnowledge.relationProperties')}
                </span>
              </div>
            }
          >
            <Table
              columns={propertyColumns}
              dataSource={relation.properties}
              rowKey="key"
              pagination={false}
              size="middle"
              showHeader={false}
            />
          </Card>

          {/* Source Text Snippets */}
          <Card
            className="yx-card"
            style={{ borderRadius: 10, border: '1px solid var(--yx-border-light)', boxShadow: '0 1px 4px rgba(0,0,0,0.04)' }}
            styles={{ body: { padding: 24 } }}
            title={
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <FileTextOutlined style={{ color: '#f97316' }} />
                <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--yx-brand-dark)' }}>
                  {t('domainKnowledge.sourceSnippets')}
                </span>
              </div>
            }
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {relation.sourceSnippets.map((snippet, idx) => (
                <div
                  key={idx}
                  style={{
                    padding: '14px 16px',
                    borderRadius: 8,
                    background: '#fffbeb',
                    border: '1px solid #fef3c7',
                    fontSize: 13,
                    color: '#475569',
                    lineHeight: 1.7,
                    borderLeft: '3px solid #f59e0b',
                  }}
                >
                  <FileTextOutlined style={{ color: '#f59e0b', marginRight: 8, fontSize: 12 }} />"{snippet}"
                </div>
              ))}
            </div>
          </Card>
        </div>
      </PageContent>
    </>
  );
}
