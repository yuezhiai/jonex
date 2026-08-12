import { redirect } from 'react-router-dom';
import { isEmbedded } from '@jonex/shell-sdk';
import loadableComponent from '@/utils/loadable';

const BasicLayout = loadableComponent(() => import('@/components/BasicLayout'));
const HostedLayout = loadableComponent(() => import('@/components/HostedLayout'));
const KnowledgeSearch = loadableComponent(() => import('@/pages/KnowledgeSearch'));
const DomainSpace = loadableComponent(() => import('@/pages/DomainSpace'));
const DomainSpaceCreate = loadableComponent(() => import('@/pages/DomainSpaceCreate'));
const DomainSpaceSettings = loadableComponent(() => import('@/pages/DomainSpaceSettings'));
const DomainSpacePermission = loadableComponent(() => import('@/pages/DomainSpacePermission'));
const DomainKnowledge = loadableComponent(() => import('@/pages/DomainKnowledge'));
const DomainKnowledgeDetail = loadableComponent(() => import('@/pages/DomainKnowledgeDetail'));
const DomainKnowledgeBlank = loadableComponent(() => import('@/pages/DomainKnowledgeBlank'));
const DomainKnowledgeDatasourceManual = loadableComponent(() => import('@/pages/DomainKnowledgeDatasourceManual'));
const DomainKnowledgeDatasourceStorage = loadableComponent(() => import('@/pages/DomainKnowledgeDatasourceStorage'));
const DomainKnowledgeDatasourceSync = loadableComponent(() => import('@/pages/DomainKnowledgeDatasourceSync'));
const DomainKnowledgeDatasourceApiPush = loadableComponent(() => import('@/pages/DomainKnowledgeDatasourceApiPush'));
const DomainKnowledgeSourceData = loadableComponent(() => import('@/pages/DomainKnowledgeSourceData'));
const DomainKnowledgeCompileResults = loadableComponent(() => import('@/pages/DomainKnowledgeCompileResults'));
const DomainKnowledgeDocumentResult = loadableComponent(() => import('@/pages/DomainKnowledgeDocumentResult'));
const DomainKnowledgeDataSource = loadableComponent(() => import('@/pages/DomainKnowledgeDataSource'));
const DomainKnowledgeParser = loadableComponent(() => import('@/pages/DomainKnowledgeParser'));
const DomainKnowledgeEngine = loadableComponent(() => import('@/pages/DomainKnowledgeEngine'));
const DomainKnowledgeGraph = loadableComponent(() => import('@/pages/DomainKnowledgeGraph'));
const DomainKnowledgeInstanceDetail = loadableComponent(() => import('@/pages/DomainKnowledgeInstanceDetail'));
const DomainKnowledgeRelationDetail = loadableComponent(() => import('@/pages/DomainKnowledgeRelationDetail'));
const DomainKnowledgeInstanceList = loadableComponent(() => import('@/pages/DomainKnowledgeInstanceList'));
const KnowledgeTracking = loadableComponent(() => import('@/pages/KnowledgeTracking'));
const DomainKnowledgeRelationList = loadableComponent(() => import('@/pages/DomainKnowledgeRelationList'));
const DomainManagement = loadableComponent(() => import('@/pages/DomainManagement'));
const DomainManagementServices = loadableComponent(() => import('@/pages/DomainManagementServices'));
const DomainManagementSearch = loadableComponent(() => import('@/pages/DomainManagementSearch'));
const NotFound = loadableComponent(() => import('@/pages/NotFound'));

export function getRoutes(mode: 'standalone' | 'hosted' = 'standalone', t?: (key: string) => string) {
  // hosted (MF mount) or embed (Shell iframe embed, URL with ?embed=1) both use shell-less layout,
  // only render the content area, shell (sidebar/header) is handled by Shell.
  const inIframe = typeof window !== 'undefined' && window.parent !== window;
  const Layout = mode === 'hosted' || (isEmbedded() && inIframe) ? HostedLayout : BasicLayout;
  const T = t || ((s: string) => s);

  return [
    {
      path: '/',
      loader: () => redirect('/knowledge-search'),
    },
    {
      path: 'home',
      loader: () => redirect('/knowledge-search'),
    },
    {
      path: '',
      element: Layout,
      children: [
        {
          path: 'knowledge-search',
          element: KnowledgeSearch,
          title: T('knowledgeSearch.pageTitle'),
          menu: { icon: 'SearchOutlined', order: 1, roles: ['admin', 'user'] },
        },
        {
          path: 'domain-space',
          element: DomainSpace,
          title: T('domainSpace.management'),
        },
        { path: 'domain-space/new', element: DomainSpaceCreate, title: T('domainSpace.create') },
        { path: 'domain-space/:id/settings', element: DomainSpaceSettings, title: T('route.domainSpaceSettings') },
        { path: 'domain-space/permissions', element: DomainSpacePermission, title: T('domainPermission.title') },
        {
          path: 'domain-knowledge',
          element: DomainKnowledge,
          title: T('domainKnowledge.management'),
          menu: { icon: 'DatabaseOutlined', order: 3, roles: ['admin', 'user'] },
        },
        { path: 'domain-knowledge/:id', element: DomainKnowledgeBlank, title: T('domainKnowledge.detail') },
        {
          path: 'domain-knowledge/:id/detail',
          element: DomainKnowledgeDetail,
          title: T('route.domainKnowledgeSettings'),
        },
        {
          path: 'domain-knowledge/:id/datasource/manual',
          element: DomainKnowledgeDatasourceManual,
          title: T('route.datasourceManual'),
        },
        {
          path: 'domain-knowledge/:id/datasource/storage/:dsId',
          element: DomainKnowledgeDatasourceStorage,
          title: T('route.datasourceStorage'),
        },
        {
          path: 'domain-knowledge/:id/datasource/sync/:dsId',
          element: DomainKnowledgeDatasourceSync,
          title: T('route.datasourceSync'),
        },
        {
          path: 'domain-knowledge/:id/datasource/api-push/:dsId',
          element: DomainKnowledgeDatasourceApiPush,
          title: T('route.datasourceApiPush'),
        },
        {
          path: 'domain-knowledge/:id/source-data',
          element: DomainKnowledgeSourceData,
          title: T('knowledgeSource.title'),
        },
        {
          path: 'domain-knowledge/:id/compile-results',
          element: DomainKnowledgeCompileResults,
          title: T('route.compileResults'),
        },
        {
          path: 'domain-knowledge/:id/documents/:docId/result',
          element: DomainKnowledgeDocumentResult,
          title: T('route.documentResult'),
        },
        {
          path: 'domain-knowledge/:id/data-source',
          element: DomainKnowledgeDataSource,
          title: T('route.dataSourceConfig'),
        },
        { path: 'domain-knowledge/:id/parser', element: DomainKnowledgeParser, title: T('route.parserConfig') },
        { path: 'domain-knowledge/:id/engine', element: DomainKnowledgeEngine, title: T('route.engineConfig') },
        {
          path: 'domain-knowledge/:id/graph',
          element: DomainKnowledgeGraph,
          title: T('domainKnowledge.graphBreadcrumb'),
        },
        {
          path: 'domain-knowledge/:id/graph/instances/:instanceId',
          element: DomainKnowledgeInstanceDetail,
          title: T('route.instanceDetail'),
        },
        {
          path: 'domain-knowledge/:id/graph/relations/:relationId',
          element: DomainKnowledgeRelationDetail,
          title: T('route.relationDetail'),
        },
        {
          path: 'domain-knowledge/:id/result/instances/:entityType',
          element: DomainKnowledgeInstanceList,
          title: T('route.ontologyInstanceDetail'),
        },
        { path: 'domain-knowledge/:id/tracking', element: KnowledgeTracking, title: T('route.tracking') },
        {
          path: 'domain-knowledge/:id/result/relations/:relationName',
          element: DomainKnowledgeRelationList,
          title: T('route.relationInstanceList'),
        },
        {
          path: 'domain-management',
          element: DomainManagement,
          title: T('domainManagement.title'),
          menu: { icon: 'ClusterOutlined', order: 4, roles: ['admin'] },
        },
        { path: 'domain-management/services', element: DomainManagementServices, title: T('route.services') },
        { path: 'domain-management/search', element: DomainManagementSearch, title: T('route.domainManagementSearch') },
      ],
    },
    { path: '*', element: NotFound, title: T('common.pageNotFound') },
  ];
}

export default getRoutes('standalone');
