import React, { useEffect, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Breadcrumb } from 'antd';
import { ReloadOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { getOntologyStatistics } from '@/api/domainKnowledge';
import type { OntologyStatistics } from '@/types/domainKnowledge';
import KnowledgeGraphPanel from '@/components/KnowledgeGraphPanel';

export default function DomainKnowledgeGraph() {
  const { t } = useTranslation();
  const { id: kbId } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [stats, setStats] = useState<OntologyStatistics | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const loadStats = useCallback(async () => {
    if (!kbId) return;
    try {
      const statsData = await getOntologyStatistics(kbId);
      setStats(statsData);
    } catch {
      // stats 加载失败不影响图谱主体
    }
  }, [kbId]);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  const handleRefresh = () => {
    loadStats();
    setRefreshKey((k) => k + 1);
  };

  return (
    <>
      {/* 面包屑 */}
      <div style={{ marginBottom: 8 }}>
        <Breadcrumb
          items={[
            {
              title: (
                <a onClick={() => navigate('/domain-knowledge')} style={{ color: 'var(--yx-text-secondary)' }}>
                  {t('domainKnowledge.management')}
                </a>
              ),
            },
            {
              title: (
                <a onClick={() => navigate(`/domain-knowledge/${kbId}`)} style={{ color: 'var(--yx-text-secondary)' }}>
                  {t('domainKnowledge.detail')}
                </a>
              ),
            },
            {
              title: <span style={{ color: 'var(--yx-brand-dark)', fontWeight: 500 }}>{t('domainKnowledge.graphBreadcrumb')}</span>,
            },
          ]}
        />
      </div>

      <PageHeader
        backTo={() => navigate(`/domain-knowledge/${kbId}`)}
        title={t('domainKnowledge.graphTitle')}
        extra={
          <Button icon={<ReloadOutlined />} size="small" onClick={handleRefresh}>
            {t('common.refresh')}
          </Button>
        }
        style={{ marginBottom: 4 }}
      />
      <div style={{ fontSize: 13, color: 'var(--yx-text-muted)', marginBottom: 20 }}>
        {stats
          ? t('domainKnowledge.graphStats', {
              name: stats.knowledge_base_name || '',
              instances: stats.ontology_instance_count,
              relations: stats.ontology_relation_count,
            })
          : t('common.loading')}
      </div>

      <PageContent showCard={false}>
        {/* 主体 */}
        <div style={{ display: 'flex', gap: 20, minHeight: 600 }}>
          <KnowledgeGraphPanel key={refreshKey} kbId={kbId!} />

          {/* 右侧边栏 */}
          <Card
            style={{
              width: 220,
              flexShrink: 0,
              borderRadius: 10,
              border: '1px solid var(--yx-border-light)',
              boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
            }}
            styles={{ body: { padding: 20 } }}
          >
            <h4
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: 'var(--yx-brand-dark)',
                marginBottom: 14,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
              }}
            >
              <InfoCircleOutlined style={{ color: 'var(--yx-accent)' }} /> {t('domainKnowledge.graphSidebarTitle')}
            </h4>
            {stats ? (
              <>
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    fontSize: 12,
                    padding: '4px 0',
                    color: 'var(--yx-text-secondary)',
                  }}
                >
                  <span>{t('domainKnowledge.graphEntityTotal')}</span>
                  <span style={{ fontWeight: 600, color: 'var(--yx-brand-dark)' }}>{stats.ontology_instance_count}</span>
                </div>
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    fontSize: 12,
                    padding: '4px 0',
                    color: 'var(--yx-text-secondary)',
                  }}
                >
                  <span>{t('domainKnowledge.graphRelationTotal')}</span>
                  <span style={{ fontWeight: 600, color: 'var(--yx-brand-dark)' }}>{stats.ontology_relation_count}</span>
                </div>
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    fontSize: 12,
                    padding: '4px 0',
                    color: 'var(--yx-text-secondary)',
                  }}
                >
                  <span>{t('domainKnowledge.graphSourceDocs')}</span>
                  <span style={{ fontWeight: 600, color: 'var(--yx-brand-dark)' }}>{stats.source_file_count}</span>
                </div>
              </>
            ) : (
              <div style={{ fontSize: 12, color: 'var(--yx-text-muted)', textAlign: 'center', padding: 20 }}>
                {t('common.loading')}
              </div>
            )}
          </Card>
        </div>
      </PageContent>
    </>
  );
}
