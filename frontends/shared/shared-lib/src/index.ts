// 共享公共库：跨子应用复用的组件与工具。
export { default as PermButton } from './components/PermButton';
export type { PermButtonProps } from './components/PermButton';
export { default as usePermission } from './hooks/usePermission';
export type { UsePermissionResult } from './hooks/usePermission';

// 统一请求入口：createRequest / getData / getLangHeader / ApiClient / ApiResponse / ApiError
export { createRequest, getData, getLangHeader } from './api/request';
export type { ApiClient, CreateRequestOptions } from './api/request';
export type { ApiError, ApiResponse } from './api/types';

// 通用工具：路由懒加载（含 chunk 失败重载）/ 存储 / 安全提示 / 文本工具
export { default as loadableComponent } from './loadable';
export { getItem, setItem, removeItem, clearAll, setCookie, getCookie, clearAllCookie } from './storage';
export { safeMessage } from './safeMessage';
export { getAvatarText, clearLocalStorageExcept } from './utils';

// 错误与时间工具：错误 message 提取 / 时间格式化
export { getErrorMessage } from './errors';
export { formatDateTime } from './datetime';

// 公共页面：404 / 403 错误页
export { default as NotFound } from './components/NotFound';
export { default as ErrorPage } from './components/ErrorPage';

// 公共页面骨架：页头（标题 + 可选同行内容）与主体（铺满剩余高度的 Card）
export { default as PageHeader } from './components/PageHeader';
export type { PageHeaderProps } from './components/PageHeader';
export { default as PageContent } from './components/PageContent';
export type { PageContentProps } from './components/PageContent';

// 应用首页入口宫格：图标 + 标题 + 说明，自适应列数
export { default as EntryGrid } from './components/EntryGrid';
export type { EntryGridItem, EntryGridProps } from './components/EntryGrid';

// 页面内分区与 KPI 卡组：标题行 + 内容外壳 / 指标卡
export { default as SectionCard } from './components/SectionCard';
export type { SectionCardProps } from './components/SectionCard';
export { default as KpiCards } from './components/KpiCards';
export type { KpiCardItem, KpiCardsProps } from './components/KpiCards';

// 危险操作二次确认：全平台删除类弹窗统一走这里
export { default as DangerConfirmModal } from './components/DangerConfirmModal';
export type { DangerConfirmModalProps } from './components/DangerConfirmModal';

// 可执行工具的运行时配置表单段（平台工具目录 / 知识库工具共用）
export {
  default as ApiRuntimeFields,
  RUNTIME_FIELD_NAMES,
  formValuesToRuntime,
  runtimeToFormValues,
} from './components/ApiRuntimeFields';
export type {
  ApiRuntimeFieldsProps,
  ApiRuntimeFormValues,
  AuthType as ToolRuntimeAuthType,
  HttpMethod as ToolRuntimeHttpMethod,
  RuntimeSubmitPart,
  RuntimeType as ToolRuntimeType,
} from './components/ApiRuntimeFields';

// 列表页组件：筛选区 + 取数 + 表格 + 三态 + 分页
export { default as CommonTable } from './components/CommonTable';
export type { CommonTableProps, CommonTableRef } from './components/CommonTable/types';
export type {
  CommonTableActions,
  FilterConfig,
  FilterContext,
  TableQueryParams,
} from './components/CommonTable/types';
