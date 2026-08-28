import { redirect } from 'react-router-dom';
import { isEmbedded } from '@jonex/shell-sdk';
import loadableComponent from '@/utils/loadable';

const BasicLayout = loadableComponent(() => import('@/components/BasicLayout'));
const HostedLayout = loadableComponent(() => import('@/components/HostedLayout'));
const Home = loadableComponent(() => import('@/pages/Home'));
const ModelAdapter = loadableComponent(() => import('@/pages/ModelAdapter'));
const TenantManagement = loadableComponent(() => import('@/pages/TenantManagement'));
const UserManagement = loadableComponent(() => import('@/pages/UserManagement'));
const RolePermission = loadableComponent(() => import('@/pages/RolePermission'));
const TaskSchedule = loadableComponent(() => import('@/pages/TaskSchedule'));
const SystemConfig = loadableComponent(() => import('@/pages/SystemConfig'));
const OperationLog = loadableComponent(() => import('@/pages/OperationLog'));
const DataAccess = loadableComponent(() => import('@/pages/DataAccess'));
const ParserManagement = loadableComponent(() => import('@/pages/ParserManagement'));
const KnowledgeCompile = loadableComponent(() => import('@/pages/KnowledgeCompile'));
const KnowledgeCompileSearch = loadableComponent(() => import('@/pages/KnowledgeCompileSearch'));
const KnowledgeCompileGraph = loadableComponent(() => import('@/pages/KnowledgeCompileGraph'));
const KnowledgeCompileVector = loadableComponent(() => import('@/pages/KnowledgeCompileVector'));
const KnowledgeCompileCompile = loadableComponent(() => import('@/pages/KnowledgeCompileCompile'));
const ErrorPage = loadableComponent(() => import('@/pages/Error'));
const NotFound = loadableComponent(() => import('@/pages/NotFound'));

export function getRoutes(mode: 'standalone' | 'hosted' = 'standalone', t?: (key: string) => string) {
  const inIframe = typeof window !== 'undefined' && window.parent !== window;
  const Layout = mode === 'hosted' || (isEmbedded() && inIframe) ? HostedLayout : BasicLayout;
  const T = t || ((s: string) => s);
  return [
    { path: '/', loader: () => redirect('/model-adapter') },
    {
      path: '',
      element: Layout,
      children: [
        {
          path: 'home',
          element: Home,
          title: T('platform.homeTitle'),
          menu: { icon: 'HomeOutlined', order: 1 },
        },
        {
          path: 'model-adapter',
          element: ModelAdapter,
          title: T('navigation.modelAdapter'),
          menu: { icon: 'ApiOutlined', order: 2, permissionCode: 'model:read' },
        },
        {
          path: 'tenant-management',
          element: TenantManagement,
          title: T('navigation.tenantManagement'),
          menu: { icon: 'TeamOutlined', order: 3, permissionCode: 'platform:tenant:read' },
        },
        {
          path: 'user-management',
          element: UserManagement,
          title: T('navigation.userManagement'),
          menu: { icon: 'UserOutlined', order: 4, permissionCode: 'user:read' },
        },
        {
          path: 'role-permission',
          element: RolePermission,
          title: T('navigation.rolePermission'),
          menu: { icon: 'SafetyOutlined', order: 5, permissionCode: 'role:read' },
        },
        {
          path: 'task-schedule',
          element: TaskSchedule,
          title: T('navigation.taskSchedule'),
          menu: { icon: 'ScheduleOutlined', order: 6, permissionCode: 'platform:task:read' },
        },
        {
          path: 'system-config',
          element: SystemConfig,
          title: T('navigation.systemConfig'),
          menu: { icon: 'SettingOutlined', order: 7, permissionCode: 'platform:config:read' },
        },
        {
          path: 'operation-log',
          element: OperationLog,
          title: T('navigation.operationLog'),
          menu: { icon: 'FileTextOutlined', order: 8, permissionCode: 'platform:audit:read' },
        },
        {
          path: 'data-access',
          element: DataAccess,
          title: T('navigation.dataAccessMethods'),
          menu: { icon: 'CloudServerOutlined', order: 9, permissionCode: 'engine:read' },
        },
        {
          path: 'parser-management',
          element: ParserManagement,
          title: T('navigation.parserManagement'),
          menu: { icon: 'CodeOutlined', order: 10, permissionCode: 'engine:read' },
        },
        { path: 'knowledge-compile', element: KnowledgeCompile, title: T('navigation.knowledgeCompile') },
        { path: 'knowledge-compile/search', element: KnowledgeCompileSearch, title: T('navigation.compileSearch') },
        { path: 'knowledge-compile/graph', element: KnowledgeCompileGraph, title: T('navigation.compileGraph') },
        { path: 'knowledge-compile/vector', element: KnowledgeCompileVector, title: T('navigation.compileVector') },
        { path: 'knowledge-compile/compile', element: KnowledgeCompileCompile, title: T('navigation.compileCompile') },
      ],
    },
    // 权限守卫 redirect 目标：?page=403 显示 403（顶层无布局壳，与 404 视觉统一）
    { path: 'error', element: ErrorPage, title: T('error.403') },
    { path: '*', element: NotFound, title: '404' },
  ];
}
export default getRoutes('standalone');
