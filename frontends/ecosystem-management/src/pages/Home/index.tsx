import React from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { ApiOutlined, ShopOutlined } from '@ant-design/icons';
import { EntryGrid, PageContent, PageHeader } from '@jonex/shared-lib';
import type { EntryGridItem } from '@jonex/shared-lib';

export default function Home() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const actions: EntryGridItem[] = [
    {
      key: '/adapter-management',
      label: t('ecosystem.adapterList'),
      description: t('ecosystem.adapterDesc'),
      path: '/adapter-management',
      icon: <ApiOutlined />,
      color: 'var(--yx-accent)',
    },
    {
      key: '/business-marketplace',
      label: t('ecosystem.businessDomainMarketplace'),
      description: t('ecosystem.marketplaceDesc'),
      path: '/business-marketplace',
      icon: <ShopOutlined />,
      color: '#10b981',
      upcoming: true,
    },
  ];
  return (
    <>
      <PageHeader
        title={t('navigation.ecosystemManagement')}
        subtitle={t('ecosystem.ecosystemManagementDesc')}
      />
      <PageContent>
        <EntryGrid items={actions} onNavigate={navigate} upcomingText={t('ecosystem.futureTag')} />
      </PageContent>
    </>
  );
}
