import React, { useCallback, useEffect, useState } from 'react';
import { useKbPermission } from '@/hooks/useKbPermission';
import { useTranslation } from 'react-i18next';
import { Button, Card, Input, Space, message, Spin, Modal, Typography, Alert } from 'antd';
import { ArrowLeftOutlined, ApiOutlined, KeyOutlined } from '@ant-design/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { getDataSource, listDataSourceDocuments, resetIngestKey } from '@/api/dataSource';
import type { DataSourceInstance } from '@/types/dataSource';
import type { ManualDocItem } from '@/types/domainKnowledge';
import DataSourceDocTable from '@/components/datasource/DataSourceDocTable';

export default function DomainKnowledgeDatasourceApiPush() {
  const { t } = useTranslation();
  const { id, dsId } = useParams();
  const { canWrite } = useKbPermission(id);
  const navigate = useNavigate();

  const [ds, setDs] = useState<DataSourceInstance | null>(null);
  const [docs, setDocs] = useState<ManualDocItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [newKey, setNewKey] = useState<string | null>(null);

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

  const ingestUrl = `${window.location.origin}/api/v1/knowledge-base/ingest/${dsId}`;
  const cfg = ds?.configJson || {};

  const onResetKey = () => {
    if (!dsId) return;
    Modal.confirm({
      title: t('domainKnowledge.apiPush.resetKeyTitle'),
      content: t('domainKnowledge.apiPush.resetKeyContent'),
      okText: t('domainKnowledge.apiPush.resetKeyOkText'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        const updated = await resetIngestKey(dsId);
        setNewKey(updated.ingestKey || null);
        message.success(t('domainKnowledge.apiPush.keyResetSuccess'));
      },
    });
  };

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
              background: '#eff6ff',
              color: '#3b82f6',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 22,
              flexShrink: 0,
            }}
          >
            <ApiOutlined />
          </div>
          <div style={{ flex: 1 }}>
            <h2 style={{ fontSize: 18, fontWeight: 700, color: '#0b2b5c', margin: 0 }}>{ds?.name || '—'}</h2>
            <div style={{ display: 'flex', gap: 16, marginTop: 4, fontSize: 13, color: '#64748b', flexWrap: 'wrap' }}>
              <span>{t('domainKnowledge.apiPush.typeLabel')}</span>
              <span>{t('domainKnowledge.apiPush.receivedDocs', { count: ds?.documentCount ?? 0 })}</span>
            </div>
          </div>
          <Button
            icon={<KeyOutlined />}
            disabled={!canWrite}
            title={canWrite ? undefined : t('domainSpace.noManagePermission')}
            onClick={onResetKey}
          >
            {t('domainKnowledge.apiPush.resetKey')}
          </Button>
        </Card>

        {newKey && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 20 }}
            message={t('domainKnowledge.apiPush.newKeyAlert')}
            description={
              <Typography.Text copyable code>
                {newKey}
              </Typography.Text>
            }
            closable
            onClose={() => setNewKey(null)}
          />
        )}

        <Card
          style={{
            borderRadius: 14,
            marginBottom: 20,
            border: '1px solid #eef2f6',
            boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
          }}
          bodyStyle={{ padding: 24 }}
        >
          <h3 style={{ margin: '0 0 16px', fontSize: 15, fontWeight: 600, color: '#0b2b5c' }}>
            {t('domainKnowledge.apiPush.connectionInfo')}
          </h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, fontSize: 13 }}>
            <div>
              <span style={{ color: '#94a3b8', marginRight: 8 }}>{t('domainKnowledge.apiPush.ingestEndpoint')}</span>
              <Typography.Text copyable>{ingestUrl}</Typography.Text>
            </div>
            <div>
              <span style={{ color: '#94a3b8', marginRight: 8 }}>{t('domainKnowledge.apiPush.apiKeyLabel')}</span>
              <span style={{ color: '#64748b' }}>{t('domainKnowledge.apiPush.keyHiddenHint')}</span>
            </div>
            <div>
              <span style={{ color: '#94a3b8', marginRight: 8 }}>{t('domainKnowledge.apiPush.allowedTypes')}</span>
              <span>{(cfg.allowed_ext || []).join('、') || '-'}</span>
            </div>
            <div>
              <span style={{ color: '#94a3b8', marginRight: 8 }}>{t('domainKnowledge.apiPush.maxFileSize')}</span>
              <span>{cfg.max_file_mb || 50} MB</span>
            </div>
            <div>
              <span style={{ color: '#94a3b8', marginRight: 8 }}>{t('domainKnowledge.apiPush.curlExampleLabel')}</span>
              <Typography.Text copyable code style={{ fontSize: 12 }}>
                {`curl -X POST "${ingestUrl}" -H "X-Ingest-Key: <API Key>" -F "file=@./doc.pdf"`}
              </Typography.Text>
            </div>
          </div>
        </Card>

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
                {t('domainKnowledge.apiPush.documentList')}
              </span>
              <Input
                placeholder={t('common.keywordSearch')}
                style={{ width: 240 }}
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
              />
            </Space>
            <span style={{ fontSize: 12, color: '#94a3b8' }}>{t('domainKnowledge.apiPush.externalPushDesc')}</span>
          </div>
          <div style={{ padding: '0 8px 8px' }}>
            <DataSourceDocTable
              kbId={id || ''}
              docs={filtered}
              loading={loading}
              onChanged={reload}
              emptyText={t('domainKnowledge.apiPush.emptyDocs')}
            />
          </div>
        </Card>
      </Spin>
    </div>
  );
}
