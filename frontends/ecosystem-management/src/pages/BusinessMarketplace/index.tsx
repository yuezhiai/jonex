import React from 'react';
import { useTranslation } from 'react-i18next';
import { Tag } from 'antd';
import { BlockOutlined, InboxOutlined, CodepenOutlined, NodeIndexOutlined } from '@ant-design/icons';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import './index.css';

const previewCards = [
  {
    icon: <BlockOutlined />,
    title: 'ecosystem.marketplaceDomainTemplates',
    desc: 'ecosystem.marketplaceDomainTemplatesDesc',
  },
  {
    icon: <InboxOutlined />,
    title: 'ecosystem.marketplaceKnowledgePackages',
    desc: 'ecosystem.marketplaceKnowledgePackagesDesc',
  },
  {
    icon: <CodepenOutlined />,
    title: 'ecosystem.marketplacePretrainedModels',
    desc: 'ecosystem.marketplacePretrainedModelsDesc',
  },
  {
    icon: <NodeIndexOutlined />,
    title: 'ecosystem.marketplaceIndustrySolutions',
    desc: 'ecosystem.marketplaceIndustrySolutionsDesc',
  },
];

export default function BusinessMarketplace() {
  const { t } = useTranslation();
  return (
    <>
      <PageHeader
        title={t('ecosystem.businessDomainMarketplace')}
        subtitle={t('ecosystem.marketplaceComingSoonDesc')}
      />
      <PageContent showCard={false} height="content">
        <div className="marketplace-grid">
          {previewCards.map((card) => (
            <div className="yx-card marketplace-card" key={card.title}>
              <span className="marketplace-card-icon" aria-hidden>
                {card.icon}
              </span>
              <h3 className="marketplace-card-title">{t(card.title)}</h3>
              <p className="marketplace-card-desc">{t(card.desc)}</p>
              <Tag className="marketplace-card-tag">{t('ecosystem.comingSoon')}</Tag>
            </div>
          ))}
        </div>
      </PageContent>
    </>
  );
}
