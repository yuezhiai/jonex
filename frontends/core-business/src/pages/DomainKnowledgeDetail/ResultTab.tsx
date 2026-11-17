import React from 'react';
import { Button, Table } from 'antd';
import { ShareAltOutlined, EyeOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router-dom';
import type { OntologyInstanceSummary, RelationInstanceSummary, OntologyStatistics } from '@/types/domainKnowledge';

interface ResultTabProps {
  resultStats: OntologyStatistics | null;
  ontologySummaries: OntologyInstanceSummary[];
  relationSummaries: RelationInstanceSummary[];
  resultLoading: boolean;
}

export default function ResultTab({
  resultStats,
  ontologySummaries,
  relationSummaries,
  resultLoading,
}: ResultTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();

  const statItems = resultStats
    ? [
        {
          label: t('domainKnowledge.sourceFilesCount'),
          value: resultStats.source_file_count.toLocaleString(),
          color: '#f97316',
        },
        {
          label: t('domainKnowledge.ontologyInstanceCount'),
          value: resultStats.ontology_instance_count.toLocaleString(),
          color: 'var(--yx-accent)',
        },
        {
          label: t('domainKnowledge.ontologyRelationCount'),
          value: resultStats.ontology_relation_count.toLocaleString(),
          color: '#10b981',
        },
      ]
    : [
        { label: t('domainKnowledge.sourceFilesCount'), value: '--', color: '#f97316' },
        { label: t('domainKnowledge.ontologyInstanceCount'), value: '--', color: 'var(--yx-accent)' },
        { label: t('domainKnowledge.ontologyRelationCount'), value: '--', color: '#10b981' },
      ];

  return (
    <div>
      <div className="yx-kb-stat-grid">
        {statItems.map((s) => (
          <div key={s.label} className="yx-kb-stat-card">
            <div style={{ fontSize: 28, fontWeight: 700, color: s.color }}>{s.value}</div>
            <div className="yx-kb-stat-label">{s.label}</div>
          </div>
        ))}
      </div>

      <div className="yx-kb-section-card-mb">
        <h3 className="yx-kb-section-title-mb">
          <ShareAltOutlined className="yx-kb-icon-blue" /> {t('domainKnowledge.ontologyInstances')}{' '}
          <span className="yx-kb-sub-text">{t('domainKnowledge.ontologyInstanceDesc')}</span>
        </h3>
        <p className="yx-kb-section-desc">{t('domainKnowledge.ontologyInstanceHelp')}</p>
        <Table
          columns={[
            {
              title: t('domainKnowledge.name'),
              dataIndex: 'display_name',
              key: 'display_name',
              render: (v: string, r: OntologyInstanceSummary) => (
                <span style={{ fontWeight: 600, color: 'var(--yx-brand-dark)' }}>{v || r.name}</span>
              ),
            },
            {
              title: t('domainKnowledge.description'),
              dataIndex: 'description',
              key: 'description',
              render: (v: string) => <span style={{ color: 'var(--yx-text-secondary)' }}>{v || '—'}</span>,
            },
            {
              title: t('domainKnowledge.buildStatus'),
              dataIndex: 'build_status',
              key: 'build_status',
              render: (v: string) => (
                <span
                  style={{
                    display: 'inline-block',
                    padding: '2px 10px',
                    borderRadius: 12,
                    fontSize: 12,
                    fontWeight: 500,
                    background: v === 'built' ? 'var(--yx-success-bg)' : '#fef3c7',
                    color: v === 'built' ? '#059669' : '#d97706',
                  }}
                >
                  {v === 'built' ? t('compile.built') : v === 'empty' ? t('compile.notBuilt') : v}
                </span>
              ),
            },
            {
              title: t('domainKnowledge.instances'),
              dataIndex: 'instance_count',
              key: 'instance_count',
              render: (v: number) => <span className="yx-kb-instance-count">{(v ?? 0).toLocaleString()}</span>,
            },
            {
              title: t('common.actions'),
              key: 'actions',
              width: 120,
              render: (_: unknown, record: OntologyInstanceSummary) => (
                <a
                  className="yx-table-action"
                  onClick={() =>
                    navigate(`/domain-knowledge/${id}/result/instances/${encodeURIComponent(record.name)}`)
                  }
                >
                  {t('domainKnowledge.viewDetails')}
                </a>
              ),
            },
          ]}
          dataSource={ontologySummaries}
          rowKey="name"
          pagination={false}
          size="middle"
          loading={resultLoading}
        />
      </div>

      <div className="yx-kb-section-card-mb">
        <h3 className="yx-kb-section-title-mb">
          <ShareAltOutlined className="yx-kb-icon-purple" /> {t('domainKnowledge.relationInstances')}{' '}
          <span className="yx-kb-sub-text">{t('domainKnowledge.relationInstanceDesc')}</span>
        </h3>
        <p className="yx-kb-section-desc">{t('domainKnowledge.relationInstanceHelp')}</p>
        <Table
          columns={[
            {
              title: t('compile.relation.sourceObject'),
              dataIndex: 'source_display_name',
              key: 'source_display_name',
              render: (v: string, r: RelationInstanceSummary) => (
                <span className="yx-kb-chip-blue">{v || r.source || '—'}</span>
              ),
            },
            {
              title: t('compile.relation.name'),
              dataIndex: 'display_name',
              key: 'display_name',
              render: (v: string, r: RelationInstanceSummary) => (
                <strong style={{ color: 'var(--yx-brand-dark)' }}>{v || r.name}</strong>
              ),
            },
            {
              title: t('compile.relation.targetObject'),
              dataIndex: 'target_display_name',
              key: 'target_display_name',
              render: (v: string, r: RelationInstanceSummary) => (
                <span className="yx-kb-chip-green">{v || r.target || '—'}</span>
              ),
            },
            {
              title: t('compile.relation.description'),
              dataIndex: 'description',
              key: 'description',
              render: (v: string) => <span style={{ fontSize: 13, color: 'var(--yx-text-secondary)' }}>{v || '—'}</span>,
            },
            {
              title: t('domainKnowledge.relationType'),
              dataIndex: 'cardinality',
              key: 'cardinality',
              render: (v: string) => {
                const cm: Record<string, { bg: string; color: string }> = {
                  many_to_one: { bg: 'var(--yx-info-bg)', color: 'var(--yx-accent)' },
                  many_to_many: { bg: '#fef3c7', color: '#d97706' },
                  one_to_one: { bg: 'var(--yx-success-bg)', color: '#059669' },
                  one_to_many: { bg: 'var(--yx-info-bg)', color: 'var(--yx-accent)' },
                };
                const lm: Record<string, string> = {
                  many_to_one: t('compile.cardinality.manyToOne'),
                  many_to_many: t('compile.cardinality.manyToMany'),
                  one_to_one: t('compile.cardinality.oneToOne'),
                  one_to_many: t('compile.cardinality.oneToMany'),
                };
                const c = cm[v] || { bg: 'var(--yx-bg-muted)', color: 'var(--yx-text-secondary)' };
                return (
                  <span
                    style={{
                      padding: '2px 8px',
                      borderRadius: 6,
                      background: c.bg,
                      color: c.color,
                      fontSize: 12,
                      fontWeight: 500,
                    }}
                  >
                    {lm[v] || v}
                  </span>
                );
              },
            },
            {
              title: t('domainKnowledge.buildStatus'),
              dataIndex: 'build_status',
              key: 'build_status',
              render: (v: string) => (
                <span
                  style={{
                    display: 'inline-block',
                    padding: '2px 10px',
                    borderRadius: 12,
                    fontSize: 12,
                    fontWeight: 500,
                    background: v === 'built' ? 'var(--yx-success-bg)' : '#fef3c7',
                    color: v === 'built' ? '#059669' : '#d97706',
                  }}
                >
                  {v === 'built' ? t('compile.built') : v === 'empty' ? t('compile.notBuilt') : v}
                </span>
              ),
            },
            {
              title: t('domainKnowledge.instances'),
              dataIndex: 'instance_count',
              key: 'instance_count',
              render: (v: number) => <span className="yx-kb-instance-count">{(v ?? 0).toLocaleString()}</span>,
            },
            {
              title: t('common.actions'),
              key: 'actions',
              width: 120,
              render: (_: unknown, record: RelationInstanceSummary) => (
                <a
                  className="yx-table-action"
                  onClick={() =>
                    navigate(`/domain-knowledge/${id}/result/relations/${encodeURIComponent(record.name)}`)
                  }
                >
                  {t('domainKnowledge.viewDetails')}
                </a>
              ),
            },
          ]}
          dataSource={relationSummaries}
          rowKey="name"
          pagination={false}
          size="middle"
          loading={resultLoading}
        />
      </div>

      <div className="yx-kb-section-card-mb">
        <div className="yx-kb-flex-between">
          <h3 className="yx-kb-section-title">
            <ShareAltOutlined className="yx-kb-icon-purple" /> {t('domainKnowledge.ontologyGraph')}
          </h3>
          <Button
            type="primary"
            style={{ padding: '6px 20px', fontSize: 13, height: 'auto' }}
            icon={<EyeOutlined />}
            onClick={() => navigate(`/domain-knowledge/${id}/graph`)}
          >
            {t('domainKnowledge.viewFullGraph')}
          </Button>
        </div>
        <p className="yx-kb-graph-desc">{t('domainKnowledge.graphHelp')}</p>
      </div>
    </div>
  );
}
