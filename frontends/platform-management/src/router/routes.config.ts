import { redirect } from 'react-router-dom';
import { isEmbedded } from '@jonex/shell-sdk';
import { ErrorPage, NotFound, loadableComponent } from '@jonex/shared-lib';

const BasicLayout = loadableComponent(() => import('@/components/BasicLayout'));
const HostedLayout = loadableComponent(() => import('@/components/HostedLayout'));
const Home = loadableComponent(() => import('@/pages/Home'));
const ModelAdapter = loadableComponent(() => import('@/pages/ModelAdapter'));
const TenantManagement = loadableComponent(() => import('@/pages/TenantManagement'));
const TenantDetail = loadableComponent(() => import('@/pages/TenantDetail'));
const UserManagement = loadableComponent(() => import('@/pages/UserManagement'));
const RolePermission = loadableComponent(() => import('@/pages/RolePermission'));
const TaskSchedule = loadableComponent(() => import('@/pages/TaskSchedule'));
const TaskCenter = loadableComponent(() => import('@/pages/TaskCenter'));
const SystemConfig = loadableComponent(() => import('@/pages/SystemConfig'));
const OperationLog = loadableComponent(() => import('@/pages/OperationLog'));
const PlatformMonitor = loadableComponent(() => import('@/pages/PlatformMonitor'));
const DataAccess = loadableComponent(() => import('@/pages/DataAccess'));
const ParserManagement = loadableComponent(() => import('@/pages/ParserManagement'));
const KnowledgeCompile = loadableComponent(() => import('@/pages/KnowledgeCompile'));
const KnowledgeCompileSearch = loadableComponent(() => import('@/pages/KnowledgeCompileSearch'));
const KnowledgeCompileGraph = loadableComponent(() => import('@/pages/KnowledgeCompileGraph'));
const KnowledgeCompileVector = loadableComponent(() => import('@/pages/KnowledgeCompileVector'));
const KnowledgeCompileCompile = loadableComponent(() => import('@/pages/KnowledgeCompileCompile'));
const DataOperations = loadableComponent(() => import('@/pages/DataOperations'));
const DataGovernance = loadableComponent(() => import('@/pages/DataGovernance'));

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
          menu: { icon: 'TeamOutlined', order: 6, permissionCode: 'platform:tenant:read' },
        },
        {
          // 租户详情：无 menu 元数据 → 不进侧边栏/不进路由菜单精确匹配守卫；
          // 面包屑/文档标题走 title；详情数据当前由路由 state + 静态种子提供（待接后端详情接口）
          path: 'tenant-management/:id',
          element: TenantDetail,
          title: T('tenantManagement.detail.title'),
        },
        {
          path: 'user-management',
          element: UserManagement,
          title: T('navigation.userManagement'),
          menu: { icon: 'UserOutlined', order: 7, permissionCode: 'user:read' },
        },
        {
          path: 'role-permission',
          element: RolePermission,
          title: T('navigation.rolePermission'),
          menu: { icon: 'SafetyOutlined', order: 8, permissionCode: 'role:read' },
        },
        {
          path: 'task-schedule',
          element: TaskSchedule,
          title: T('navigation.taskSchedule'),
          menu: { icon: 'ScheduleOutlined', order: 9, permissionCode: 'platform:task:read' },
        },
        {
          // 任务中心：任务执行实例的列表（一条 = 一次文档入库的执行记录），
          // 与上面的「任务调度」（cron 调度计划配置）是两回事，故并存
          path: 'task-center',
          element: TaskCenter,
          title: T('navigation.taskCenter'),
          // 2026-10-08 放开入口：去掉 `hidden`（`aa33fe57` 加的），侧栏重新出现该项。
          // BasicLayout 按 `permissionCode` 过滤，所以只有持 `task_center:read` 的人看得见，
          // 与 shell 顶栏的 `canSeeTaskCenter` 同码。
          //
          // 这个码与「任务调度」的 `platform:task:read` 是**两个码**（2026-10-08 分开）：
          // 两个不同页面共用一个码就无法分别授权。分开的代价写在规格 §四.3 —— 只被授了
          // `platform:task:read` 的自定义角色会失去任务中心入口，需人工补授。
          //
          // 服务端防线已在能力侧补齐（`capabilities/task_center/capability.py` 的
          // `_ACTION_PERMISSIONS` 按 read / write 判权），所以路由守卫只是第一道；
          // 两壳的守卫本来也不等价（standalone 的 basename 带尾斜杠，拦不住）。
          menu: { icon: 'ScheduleOutlined', order: 10, permissionCode: 'task_center:read' },
        },
        {
          path: 'system-config',
          element: SystemConfig,
          title: T('navigation.systemConfig'),
          // 合并页：同一个路由承载三种内容，**页面内部**按码分段渲染 ——
          //   平台信息 / 平台安全设置 ← platform:config:read
          //   租户安全设置           ← tenant:security:read
          // 所以可见性用**码数组（任一命中即可）**：租户管理员没有 platform:config:read，
          // 但必须能进这一页看到「租户安全设置」。只写平台码会让租户管理员点不进自己唯一的安全页；
          // 只写租户码则平台管理员失去系统配置入口。
          // 配套：后端菜单树里原来那条独立的 navigation.tenantSecurity 菜单已删除（见 update/047），
          // 否则它指向的路由不存在 → 侧栏出现死链。
          menu: {
            icon: 'SettingOutlined',
            order: 11,
            permissionCode: ['platform:config:read', 'tenant:security:read'],
          },
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
          menu: { icon: 'FileTextOutlined', order: 12, permissionCode: 'audit:read' },
        },
        {
          path: 'data-access',
          element: DataAccess,
          title: T('navigation.dataAccessMethods'),
          // [jonex] 权限重构 B1：engine:read → datasource:read，与 DB 菜单树 id 7 对齐。
          // 原先数据源管理与解析器管理共用 engine:read，两个不同页面一个码 → 无法分别授权。
          // 两者都是 platform scope（仅平台管理员），所以当下没有可见的行为变化，
          // 但码不一致会在「只授 datasource:read」时让页面 403，属埋雷。
          menu: { icon: 'CloudServerOutlined', order: 13, permissionCode: 'datasource:read' },
        },
        {
          path: 'parser-management',
          element: ParserManagement,
          title: T('navigation.parserManagement'),
          menu: { icon: 'CodeOutlined', order: 14, permissionCode: 'engine:read' },
        },
        {
          // 平台监控（任务 / 调用 / Token）：三个页签均已接真实接口
          // （契约 T1–T4、C1–C9 + M1、K1–K4）。取数统一在
          // `src/api/platformMonitor.ts`，那是接后端的唯一入口。
          //
          // order 4 = 紧跟在「数据运营」下面。hosted 侧栏由 DB 菜单树驱动，
          // 两侧各有一条独立菜单：standalone 用这里的 menu.order，
          // hosted 用 deploy/postgres/update/075_platform_monitor_new_menu.sql 把
          // navigation.platformMonitor 行放到平台管理组第 2 位。两处必须同批改，
          // 否则一种壳里排在上、另一种排在下。
          path: 'platform-monitor',
          element: PlatformMonitor,
          title: T('navigation.platformMonitor'),
          menu: { icon: 'MonitorOutlined', order: 4, permissionCode: 'platform:monitor:read' },
        },
        { path: 'knowledge-compile', element: KnowledgeCompile, title: T('navigation.knowledgeCompile') },
        { path: 'knowledge-compile/search', element: KnowledgeCompileSearch, title: T('navigation.compileSearch') },
        { path: 'knowledge-compile/graph', element: KnowledgeCompileGraph, title: T('navigation.compileGraph') },
        { path: 'knowledge-compile/vector', element: KnowledgeCompileVector, title: T('navigation.compileVector') },
        { path: 'knowledge-compile/compile', element: KnowledgeCompileCompile, title: T('navigation.compileCompile') },
        // 数据运营：一个页面、三个 tab（tab 落在 ?tab=）。
        // 权限码 analytics:read 是**租户级**（本租户的检索/资产统计，接口在无 domain_space_id
        // 时要求租户管理员），当前只控菜单与前端可见性，不是接口防线——见
        // deploy/postgres/update/036_data_operations_permission_and_menu.sql 的边界说明。
        // order 3 是刻意的：与 DB 菜单树同序（平台管理分组里排第一），
        // 紧随其后的是平台监控（order 4）。图标名同时注册在 router/menu.ts 的
        // IconMap（未注册会渲染成空图标）。
        {
          path: 'data-operations',
          element: DataOperations,
          title: T('navigation.dataOperations'),
          menu: { icon: 'BarChartOutlined', order: 3, permissionCode: 'analytics:read' },
        },
        // 资产目录：第 2 期数据治理。order 5 = 排在平台监控之后；
        // 新增平台监控（order 4）后，原 4..13 的菜单项统一 +1
        // （tenant 6 / user 7 / role 8 / task-schedule 9 / task-center 10 /
        // system-config 11 / operation-log 12 / data-access 13 / parser 14）。
        // 权限码复用 knowledge:read，**不新增权限码**（与 DB 菜单树 id 30 一致）。
        {
          path: 'data-governance/assets',
          element: DataGovernance,
          title: T('navigation.dataGovernance'),
          menu: { icon: 'DatabaseOutlined', order: 5, permissionCode: 'knowledge:read' },
        },
      ],
    },
    // 权限守卫 redirect 目标：?page=403 显示 403（顶层无布局壳，与 404 视觉统一）
    { path: 'error', element: ErrorPage, title: T('error.403') },
    { path: '*', element: NotFound, title: '404' },
  ];
}
export default getRoutes('standalone');
