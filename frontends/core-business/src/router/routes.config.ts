import { redirect } from 'react-router-dom';
import { isEmbedded } from '@jonex/shell-sdk';
import { ErrorPage, NotFound, loadableComponent } from '@jonex/shared-lib';

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
/** 本体图形编辑页（移植自 nexonto 的画布）：一个数据源一份 schema */
const OntologyCanvas = loadableComponent(() => import('@/features/ontologyCanvas/OntologyCanvasPage'));
const DomainKnowledgeDatasourceManual = loadableComponent(() => import('@/pages/DomainKnowledgeDatasourceManual'));
const DomainKnowledgeDatasourceStorage = loadableComponent(() => import('@/pages/DomainKnowledgeDatasourceStorage'));
const DomainKnowledgeDatasourceSync = loadableComponent(() => import('@/pages/DomainKnowledgeDatasourceSync'));
const DomainKnowledgeDatasourceApiPush = loadableComponent(() => import('@/pages/DomainKnowledgeDatasourceApiPush'));
const DomainKnowledgeSourceData = loadableComponent(() => import('@/pages/DomainKnowledgeSourceData'));
const DomainKnowledgeDocumentResult = loadableComponent(() => import('@/pages/DomainKnowledgeDocumentResult'));
const DomainKnowledgeDataSource = loadableComponent(() => import('@/pages/DomainKnowledgeDataSource'));
const DomainKnowledgeParser = loadableComponent(() => import('@/pages/DomainKnowledgeParser'));
const DomainKnowledgeModelSettings = loadableComponent(() => import('@/pages/DomainKnowledgeModelSettings'));
const DomainKnowledgeGraph = loadableComponent(() => import('@/pages/DomainKnowledgeGraph'));
const DomainKnowledgeInstanceDetail = loadableComponent(() => import('@/pages/DomainKnowledgeInstanceDetail'));
const DomainKnowledgeRelationDetail = loadableComponent(() => import('@/pages/DomainKnowledgeRelationDetail'));
const DomainKnowledgeInstanceList = loadableComponent(() => import('@/pages/DomainKnowledgeInstanceList'));
const DomainKnowledgeRelationList = loadableComponent(() => import('@/pages/DomainKnowledgeRelationList'));
const DomainManagement = loadableComponent(() => import('@/pages/DomainManagement'));
const DomainManagementServices = loadableComponent(() => import('@/pages/DomainManagementServices'));
const DomainManagementSearch = loadableComponent(() => import('@/pages/DomainManagementSearch'));

export function getRoutes(mode: 'standalone' | 'hosted' = 'standalone', t?: (key: string) => string) {
  // hosted (MF mount) or embed (Shell iframe embed, URL with ?embed=1) both use shell-less layout,
  // only render the content area, shell (sidebar/header) is handled by Shell.
  //
  // ⚠️ 三个信号之间是**或**，不是且。原先是 `isEmbedded() && inIframe`，踩过的坑：
  // 在 iframe 里导航时 `?embed=1` 会丢（子应用自己的 createBrowserRouter 跳转不带 search），
  // 但 `window.parent` 仍然是 shell —— 此时「且」判成假，回落到 BasicLayout，
  // shell 外面又套了一层自己的侧栏/顶栏（双层壳），且 BasicLayout 的内容区高度链不完整，
  // 画布 `height:100%` 撑不满、底部留白。
  // 症状是**切语言才出现**：useMemo 依赖 `t`，只有切语言才会重算 Layout 并重新求值 isEmbedded()，
  // 单纯导航不会（useMemo 不重跑，坏掉的判定一直没被触发）。
  //
  // 三个信号各自都足以说明「壳由别人提供」，任何一个成立就都该走无壳布局：
  //   mode==='hosted' —— MF 挂载，就在 shell 里
  //   inIframe        —— 被 iframe 嵌着，我们不是最外层（本产品里 iframe 只来自 shell 的 AppHost）
  //   isEmbedded()    —— URL 显式要求 embed（shell 加的参数，也允许被单独使用）
  const inIframe = typeof window !== 'undefined' && window.parent !== window;
  const Layout = mode === 'hosted' || inIframe || isEmbedded() ? HostedLayout : BasicLayout;
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
          menu: { icon: 'SearchOutlined', order: 1, permissionCode: 'knowledge:read' },
        },
        {
          path: 'domain-space',
          element: DomainSpace,
          title: T('domainSpace.management'),
          // [jonex] 权限重构 B6（方案 §12.4 / §12.3 / 执行文档 §7.4-§7.5）：
          // 路由守卫用 space:read（「能否打开页面」的读通道）——普通用户持有 space:read，
          // 所以进得来。⚠️ 不能用 space:write 守卫：后端「建空间」的准入是
          // tenant:admin/platform:admin **或** space:write（can_create_space，W2 2026-10-19），
          // 单看码会把「仅持 space:write 的租户管理员兜底路径」判漏；而空间管理者即便没有
          // space:write 也得能打开本页看自己的空间。
          // 单个空间内的编辑/删除/管成员按钮走 can_manage_permissions（资源身份字段），不在这里。
          // 建空间入口的判据统一用后端 can_create_space 字段（SpaceSwitcher 隐藏入口、
          // DomainSpaceCreate 页内拦截），前端不硬编码码名。
          // order:2 补齐 standalone 侧边栏序列（search=1 / knowledge=3 / management=4）；
          // hosted（shell）模式导航走 DB 菜单树、DB 菜单树按设计不含此项，故此改只影响 standalone。
          menu: { icon: 'BlockOutlined', order: 2, permissionCode: 'space:read' },
        },
        // /domain-space/new 若加 permissionCode 只能写码名，与后端「字段优先」口径不一致，
        // 故不在此设守卫：DomainSpaceCreate 页内按 can_create_space 拦截（含直接输 URL）。
        { path: 'domain-space/new', element: DomainSpaceCreate, title: T('domainSpace.create') },
        { path: 'domain-space/:id/settings', element: DomainSpaceSettings, title: T('route.domainSpaceSettings') },
        { path: 'domain-space/permissions', element: DomainSpacePermission, title: T('domainPermission.title') },
        {
          path: 'domain-knowledge',
          element: DomainKnowledge,
          title: T('domainKnowledge.management'),
          menu: { icon: 'DatabaseOutlined', order: 3, permissionCode: 'knowledge:read' },
        },
        { path: 'domain-knowledge/:id', element: DomainKnowledgeBlank, title: T('domainKnowledge.detail') },
        {
          // 本体图形编辑：:dsId 为具体数据源；'-'（知识库级）自 v2 起转抽象编辑器
          // （路由参数不能为空，而 data_source_id 为 NULL 是有意义的取值）
          path: 'domain-knowledge/:kbId/schema/:dsId',
          element: OntologyCanvas,
          title: T('route.ontologyCanvas'),
        },
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
        {
          path: 'domain-knowledge/:id/models',
          element: DomainKnowledgeModelSettings,
          title: T('route.modelConfig'),
        },
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
        {
          path: 'domain-knowledge/:id/result/relations/:relationName',
          element: DomainKnowledgeRelationList,
          title: T('route.relationInstanceList'),
        },
        {
          path: 'domain-management',
          element: DomainManagement,
          title: T('domainManagement.title'),
          menu: { icon: 'ClusterOutlined', order: 4, permissionCode: 'service:read' },
        },
        { path: 'domain-management/services', element: DomainManagementServices, title: T('route.services') },
        { path: 'domain-management/search', element: DomainManagementSearch, title: T('route.domainManagementSearch') },
      ],
    },
    // 权限守卫 redirect 目标：?page=403 显示 403（顶层无布局壳，与 404 视觉统一）
    { path: 'error', element: ErrorPage, title: T('error.403') },
    { path: '*', element: NotFound, title: T('common.pageNotFound') },
  ];
}

export default getRoutes('standalone');
