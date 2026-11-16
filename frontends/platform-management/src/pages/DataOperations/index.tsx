import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import DataOperationsTabs from './DataOperationsTabs';
import { toDoTabKey } from './utils';
import DataAssets from './DataAssets';
import KnowledgeAssets from './KnowledgeAssets';
import Nl2SqlAnalysis from './Nl2SqlAnalysis';
import SearchAnalysis from './SearchAnalysis';
import './index.scss';

/**
 * 数据运营：一个页面、四个 tab。
 * 三块内容各自带取数与四态，页头与 tab 栏只在这里渲染一次。
 * 页面骨架用 shared-lib 的 PageHeader + PageContent（showCard=false）——
 * 本页主体由多张分区卡片构成，再套一层白卡会变成卡中卡。
 */
export default function DataOperations() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const tab = toDoTabKey(searchParams.get('tab'));

  return (
    <>
      <PageHeader title={t('navigation.dataOperations')} />
      <PageContent showCard={false}>
        <div className="yx-do-page">
          <DataOperationsTabs
            activeKey={tab}
            panes={{
              assets: <DataAssets />,
              knowledge: <KnowledgeAssets />,
              search: <SearchAnalysis />,
              nl2sql: <Nl2SqlAnalysis />,
            }}
          />
        </div>
      </PageContent>
    </>
  );
}
