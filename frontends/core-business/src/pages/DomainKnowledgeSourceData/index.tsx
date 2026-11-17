import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Select, Space } from 'antd';
import { DownloadOutlined, SyncOutlined } from '@ant-design/icons';
import { PageContent, PageHeader } from '@jonex/shared-lib';

const stats = [{ value: '1,284' }, { value: '3.2 GB' }, { value: '256' }, { value: '99.8%' }];

const kbDefs = [
  { id: 'finance', nameKey: 'knowledgeSource.demo.financeKb' },
  { id: 'medical', nameKey: 'knowledgeSource.demo.medicalKb' },
  { id: 'equipment', nameKey: 'knowledgeSource.demo.equipmentKb' },
];

function getJsonPreview(t: (key: string) => string) {
  return `{
  "knowledge_base": "${t('knowledgeSource.demo.financeKb')}",
  "last_sync": "2026-05-21T14:30:00+08:00",
  "total_docs": 1284,
  "status": "synced",
  "documents": [
    {
      "id": "DOC-001",
      "title": "${t('knowledgeSource.demo.fundReport')}",
      "type": "PDF",
      "size": 2457600,
      "pages": 32,
      "parsed": true,
      "created_at": "2026-01-15T09:30:00Z"
    },
    {
      "id": "DOC-002",
      "title": "${t('knowledgeSource.demo.riskWhitePaper')}",
      "type": "DOCX",
      "size": 1048576,
      "pages": 18,
      "parsed": true,
      "created_at": "2026-02-20T14:00:00Z"
    }
  ],
  "categories": [
    { "name": "${t('knowledgeSource.demo.funds')}", "count": 452 },
    { "name": "${t('knowledgeSource.demo.wealthInsurance')}", "count": 328 },
    { "name": "${t('knowledgeSource.demo.credit')}", "count": 504 }
  ]
}`;
}

export default function DomainKnowledgeSourceData() {
  const { t } = useTranslation();
  const kbs = useMemo(() => kbDefs.map((kb) => ({ value: kb.id, label: t(kb.nameKey) })), [t]);
  const jsonPreview = useMemo(() => getJsonPreview(t), [t]);
  const [activeTab, setActiveTab] = useState('raw');
  const [selectedKb, setSelectedKb] = useState(kbDefs[0].id);

  const tabs = [
    { key: 'raw', label: t('knowledgeSource.tabRaw') },
    { key: 'structured', label: t('knowledgeSource.tabStructured') },
    { key: 'metadata', label: t('knowledgeSource.tabMetadata') },
  ];
  const statKeys = [
    'knowledgeSource.totalDocs',
    'knowledgeSource.dataTotal',
    'knowledgeSource.last7Days',
    'knowledgeSource.parseRate',
  ] as const;

  return (
    <>
      <PageHeader title={t('knowledgeSource.title')} style={{ marginBottom: 4 }} />
      <p style={{ color: 'var(--yx-text-secondary)', margin: '0 0 16px', fontSize: 14 }}>{t('knowledgeSource.description')}</p>
      <PageContent showCard={false}>
        <Card
          style={{ borderRadius: 10, marginBottom: 20, border: '1px solid var(--yx-border-light)' }}
          bodyStyle={{ padding: '0 20px' }}
        >
          <div style={{ display: 'flex', borderBottom: '2px solid var(--yx-border)' }}>
            {tabs.map((tab) => (
              <div
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                style={{
                  padding: '10px 24px',
                  cursor: 'pointer',
                  fontSize: 14,
                  color: activeTab === tab.key ? 'var(--yx-accent)' : 'var(--yx-text-secondary)',
                  borderBottom: activeTab === tab.key ? '2px solid #3b82f6' : '2px solid transparent',
                  marginBottom: -2,
                  fontWeight: activeTab === tab.key ? 600 : 400,
                  transition: 'all 0.2s',
                }}
              >
                {tab.label}
              </div>
            ))}
            <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center' }}>
              <Select value={selectedKb} onChange={setSelectedKb} style={{ width: 180 }} options={kbs} />
            </div>
          </div>
        </Card>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 16, marginBottom: 20 }}>
          {stats.map((s, idx) => (
            <Card
              key={statKeys[idx]}
              style={{ borderRadius: 8, textAlign: 'center', border: '1px solid var(--yx-border)' }}
              bodyStyle={{ padding: 16 }}
            >
              <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--yx-accent)' }}>{s.value}</div>
              <div style={{ fontSize: 13, color: 'var(--yx-text-secondary)', marginTop: 4 }}>{t(statKeys[idx])}</div>
            </Card>
          ))}
        </div>

        <Card style={{ borderRadius: 10, overflow: 'hidden', border: '1px solid var(--yx-border-light)' }} bodyStyle={{ padding: 0 }}>
          <div
            style={{
              padding: '12px 20px',
              background: 'var(--yx-bg-subtle)',
              borderBottom: '1px solid var(--yx-border)',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <span style={{ fontWeight: 600, fontSize: 14, color: 'var(--yx-brand-dark)' }}>{t('knowledgeSource.dataPreview')}</span>
            <Space>
              <Button size="small" icon={<DownloadOutlined />}>
                {t('common.export')}
              </Button>
              <Button size="small" icon={<SyncOutlined />}>
                {t('common.refresh')}
              </Button>
            </Space>
          </div>
          <div style={{ padding: 20 }}>
            <pre
              style={{
                background: '#1e293b',
                color: 'var(--yx-border)',
                borderRadius: 8,
                padding: 20,
                fontSize: 13,
                lineHeight: 1.7,
                overflowX: 'auto',
                whiteSpace: 'pre-wrap',
                margin: 0,
              }}
            >
              {jsonPreview}
            </pre>
          </div>
        </Card>
      </PageContent>
    </>
  );
}
