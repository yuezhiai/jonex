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
const SystemMonitor = loadableComponent(() => import('@/pages/SystemMonitor'));
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
          // [jonex] 权限重构 B1：platform:audit:read → audit:read（tenant scope）。
          // 审计日志已放开给租户管理员（方案 §8 第 3 条）：接口改为双码放行、
          // DB 菜单树 id 15 的码也改成 audit:read。这里若不同步改，租户管理员会
          // 在 shell 导航看到「操作日志」入口，点进来被 router/index.tsx 的守卫
          // 直接 redirect('/error?page=403') —— 三处（接口/菜单/路由）必须同批改。
          // 平台管理员天然持全部 tenant 码，改后仍可见可进。
          menu: { icon: 'FileTextOutlined', order: 8, permissionCode: 'audit:read' },
        },
        {
          path: 'data-access',
          element: DataAccess,
          title: T('navigation.dataAccessMethods'),
          // [jonex] 权限重构 B1：engine:read → datasource:read，与 DB 菜单树 id 7 对齐。
          // 原先数据源管理与解析器管理共用 engine:read，两个不同页面一个码 → 无法分别授权。
          // 两者都是 platform scope（仅平台管理员），所以当下没有可见的行为变化，
          // 但码不一致会在「只授 datasource:read」时让页面 403，属埋雷。
          menu: { icon: 'CloudServerOutlined', order: 9, permissionCode: 'datasource:read' },
        },
        {
          path: 'parser-management',
          element: ParserManagement,
          title: T('navigation.parserManagement'),
          menu: { icon: 'CodeOutlined', order: 10, permissionCode: 'engine:read' },
        },
        {
          // [jonex] 全局系统监控 —— 占位页（研发中）。给 platform:monitor:read 一个真实落点，
          // 避免角色权限页出现「勾了也没有入口」的权限项。见 docs/permissions/PERMISSIONS_REDESIGN.md §11.2
          path: 'system-monitor',
          element: SystemMonitor,
          title: T('navigation.systemMonitor'),
          menu: { icon: 'DashboardOutlined', order: 11, permissionCode: 'platform:monitor:read' },
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
