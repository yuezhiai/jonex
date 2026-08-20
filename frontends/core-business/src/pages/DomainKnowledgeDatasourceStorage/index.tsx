import React, { useCallback, useEffect, useState } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import { useTranslation } from 'react-i18next';
import { Button, Card, Input, Space, message, Spin } from 'antd';
import { ArrowLeftOutlined, FolderOpenOutlined, SyncOutlined } from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { getDataSource, listDataSourceDocuments, syncDataSource } from '@/api/dataSource';
import type { DataSourceInstance } from '@/types/dataSource';
import type { ManualDocItem } from '@/types/domainKnowledge';
import DataSourceDocTable from '@/components/datasource/DataSourceDocTable';

export default function DomainKnowledgeDatasourceStorage() {
  const { t } = useTranslation();
  const { id, dsId } = useParams();
  const { canWrite } = useKbPermission(id);
  const navigate = useNavigate();

  const [ds, setDs] = useState<DataSourceInstance | null>(null);
  const [docs, setDocs] = useState<ManualDocItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [keyword, setKeyword] = useState('');

  const reload = useCallback(() => {
    if (!id || !dsId) return;
    setLoading(true);
    Promise.all([getDataSource(dsId), listDataSourceDocuments(id, dsId)])
      .then(([d, list]) => {
        setDs(d);
        setDocs(list);
      })
      .catch((e: any) => message.error(e?.message || t('common.loadFailed')))
      .finally(() => setLoading(false));
  }, [id, dsId]);

  useEffect(() => {
    reload();
  }, [reload]);

  const onSync = async () => {
    if (!dsId) return;
    setSyncing(true);
    try {
      const r = await syncDataSource(dsId);
      message.success(
        r.failed
          ? t('domainKnowledge.sync.completedWithFailures', { created: r.created, failed: r.failed })
          : t('domainKnowledge.sync.completed', { created: r.created }),
      );
      reload();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.sync.failed'));
    } finally {
      setSyncing(false);
    }
  };

  const cfg = ds?.configJson || {};
  const stats = [
    { label: t('domainKnowledge.sync.totalDocuments'), value: String(ds?.documentCount ?? '--'), color: '#10b981' },
    { label: t('domainKnowledge.sync.status'), value: ds?.lastSyncStatus || t('status.unsynced'), color: '#f97316' },
    {
      label: t('domainKnowledge.sync.mode'),
      value: ds?.syncMode === 'scheduled' ? t('domainKnowledge.sync.scheduled') : t('domainKnowledge.sync.manual'),
      color: '#3b82f6',
    },
    {
      label: t('domainKnowledge.sync.lastSyncTime'),
      value: ds?.lastSyncAt ? ds.lastSyncAt.replace('T', ' ').slice(0, 19) : '—',
    },
  ];

  const filtered = docs.filter((d) => d.name.includes(keyword));

  return (
    <div>
      <a
        onClick={() => navigate(`/domain-knowledge/${id}/detail`)}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          marginBottom: 16,
          fontSize: 14,
          color: '#64748b',
          cursor: 'pointer',
        }}
      >
        <ArrowLeftOutlined /> {t('domainKnowledge.backToKnowledgeDetail')}
      </a>

      <Spin spinning={loading}>
        <Card
          style={{
            borderRadius: 14,
            marginBottom: 24,
            border: '1px solid #eef2f6',
            boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
          }}
          bodyStyle={{ padding: '20px 24px', display: 'flex', alignItems: 'center', gap: 16 }}
        >
          <div
            style={{
              width: 48,
              height: 48,
              borderRadius: 12,
              background: '#fff7ed',
              color: '#f97316',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 22,
              flexShrink: 0,
            }}
          >
            <FolderOpenOutlined />
          </div>
          <div style={{ flex: 1 }}>
            <h2 style={{ fontSize: 18, fontWeight: 700, color: '#0b2b5c', margin: 0 }}>{ds?.name || '—'}</h2>
            <div style={{ display: 'flex', gap: 16, marginTop: 4, fontSize: 13, color: '#64748b', flexWrap: 'wrap' }}>
              <span>{t('domainKnowledge.dataSourceType.storage')}</span>
              <span>{cfg.backend || '-'}</span>
              <span>
                {t('domainKnowledge.storage.bucketInfo', {
                  bucket: cfg.bucket || '-',
                  prefix: cfg.prefix ? ` / ${cfg.prefix}` : '',
                })}
              </span>
              <span>{t('domainKnowledge.docsWithCount', { count: ds?.documentCount ?? 0 })}</span>
            </div>
          </div>
          <Button
            type="primary"
            icon={<SyncOutlined />}
            loading={syncing}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={onSync}
          >
            {t('domainKnowledge.sync.instantSync')}
          </Button>
        </Card>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16, marginBottom: 20 }}>
          {stats.map((s) => (
            <Card key={s.label} style={{ borderRadius: 10, textAlign: 'center' }} bodyStyle={{ padding: 16 }}>
              <div style={{ fontSize: 16, fontWeight: 600, color: s.color || '#0b2b5c' }}>{s.value}</div>
              <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 4 }}>{s.label}</div>
            </Card>
          ))}
        </div>

        <Card
          style={{ borderRadius: 14, border: '1px solid #eef2f6', boxShadow: '0 1px 4px rgba(0,0,0,0.04)' }}
          bodyStyle={{ padding: 0 }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '16px 20px',
              borderBottom: '1px solid #eef2f6',
            }}
          >
            <Space>
              <span style={{ fontSize: 14, fontWeight: 600, color: '#0b2b5c' }}>
                {t('domainKnowledge.sync.docList')}
              </span>
              <Input
                placeholder={t('common.keywordSearch')}
                style={{ width: 240 }}
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
              />
            </Space>
            <span style={{ fontSize: 12, color: '#94a3b8' }}>{t('domainKnowledge.storage.syncDesc')}</span>
          </div>
          <div style={{ padding: '0 8px 8px' }}>
            <DataSourceDocTable
              kbId={id || ''}
              docs={filtered}
              loading={loading}
              onChanged={reload}
              emptyText={t('domainKnowledge.sync.emptyDocs')}
            />
          </div>
        </Card>
      </Spin>
    </div>
  );
}
