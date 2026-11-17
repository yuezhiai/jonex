import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Input, Button, Card, Tag, Space, Select } from 'antd';
import { SearchOutlined, StarFilled } from '@ant-design/icons';
import { PageContent, PageHeader } from '@jonex/shared-lib';

const resultList = [
  { key: 'creditRisk', date: '2026-03-15', relevance: 96 },
  { key: 'insurancePricing', date: '2026-02-20', relevance: 92 },
  { key: 'medicalFraud', date: '2026-04-10', relevance: 88 },
  { key: 'financialCompliance', date: '2026-01-08', relevance: 85 },
  { key: 'supplyChainFinance', date: '2026-05-05', relevance: 82 },
];

const chipKeys = ['all', 'finRisk', 'medInsurance', 'smartManufacturing', 'eduTraining', 'legalRegulations'];

export default function DomainManagementSearch() {
  const { t, i18n } = useTranslation();
  const [query, setQuery] = useState(t('domainManagementSearch.defaultQuery'));
  const [activeChip, setActiveChip] = useState('all');
  const localizedResults = resultList.map((item) => ({
    ...item,
    title: t(`domainManagementSearch.demo.${item.key}.title`),
    source: t(`domainManagementSearch.demo.${item.key}.source`),
    snippet: t(`domainManagementSearch.demo.${item.key}.snippet`),
  }));

  useEffect(() => {
    setQuery(t('domainManagementSearch.defaultQuery'));
  }, [i18n.language, t]);

  return (
    <>
      <PageHeader title={t('knowledgeSearch.pageTitle')} style={{ marginBottom: 4 }} />
      <p style={{ color: 'var(--yx-text-secondary)', margin: '0 0 16px', fontSize: 14 }}>{t('domainManagementSearch.subtitle')}</p>
      <PageContent showCard={false}>
        <Card style={{ borderRadius: 16, marginBottom: 24, textAlign: 'center' }} bodyStyle={{ padding: 32 }}>
          <div style={{ fontSize: 15, color: 'var(--yx-text-secondary)' }}>{t('domainManagementSearch.searchDesc')}</div>
          <div style={{ display: 'flex', maxWidth: 700, margin: '16px auto 0', gap: 8 }}>
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('domainManagementSearch.searchPlaceholder')}
              style={{ flex: 1, padding: '14px 20px', border: '2px solid var(--yx-border)', borderRadius: 12, fontSize: 16 }}
              onPressEnter={() => {}}
            />
            <Button
              type="primary"
              style={{ padding: '14px 32px', borderRadius: 12, fontSize: 15, height: 'auto' }}
              icon={<SearchOutlined />}
            >
              {t('knowledgeSearch.search')}
            </Button>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'center', marginTop: 16 }}>
            {chipKeys.map((ck) => (
              <Tag
                key={ck}
                style={{
                  padding: '6px 16px',
                  borderRadius: 20,
                  fontSize: 13,
                  cursor: 'pointer',
                  border: `1px solid ${activeChip === ck ? 'var(--yx-accent)' : '#d1d5db'}`,
                  background: activeChip === ck ? 'var(--yx-accent)' : '#fff',
                  color: activeChip === ck ? '#fff' : 'var(--yx-text-secondary)',
                }}
                onClick={() => setActiveChip(ck)}
              >
                {t(`domainManagementSearch.chip.${ck}`)}
              </Tag>
            ))}
          </div>
        </Card>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <span style={{ fontSize: 14, color: 'var(--yx-text-secondary)' }}>
            {t('domainManagementSearch.resultSummary', { count: 42, time: '0.35' })}
          </span>
          <Select
            defaultValue="relevance"
            style={{ width: 160 }}
            options={[
              { value: 'relevance', label: t('domainManagementSearch.sortByRelevance') },
              { value: 'time', label: t('domainManagementSearch.sortByTime') },
            ]}
          />
        </div>

        {localizedResults.map((r, i) => (
          <Card
            key={i}
            style={{ borderRadius: 12, marginBottom: 12, border: '1px solid var(--yx-border)' }}
            bodyStyle={{ padding: 20 }}
            hoverable
          >
            <h3 style={{ margin: '0 0 8px', fontSize: 16 }}>
              <a style={{ color: 'var(--yx-accent)' }}>{r.title}</a>
            </h3>
            <p style={{ margin: '0 0 12px', color: 'var(--yx-text-secondary)', fontSize: 14, lineHeight: 1.6 }}>{r.snippet}</p>
            <div style={{ display: 'flex', gap: 16, fontSize: 12, color: 'var(--yx-text-muted)', alignItems: 'center' }}>
              <Tag style={{ background: 'var(--yx-bg-muted)', border: 'none' }}>{r.source}</Tag>
              <span>{r.date}</span>
              <span style={{ color: '#22c55e', fontWeight: 600 }}>
                <StarFilled style={{ fontSize: 10 }} />{' '}
                {t('domainManagementSearch.relevancePercent', { relevance: r.relevance })}
              </span>
            </div>
          </Card>
        ))}

        {/* 结果分页等接入真实接口后再上 antd Pagination：现在的数据是静态演示，不放假分页器 */}
      </PageContent>
    </>
  );
}
