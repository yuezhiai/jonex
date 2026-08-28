// 共享公共库：跨子应用复用的组件与工具。
export { default as PermButton } from './components/PermButton';
export type { PermButtonProps } from './components/PermButton';
export { default as usePermission } from './hooks/usePermission';
export type { UsePermissionResult } from './hooks/usePermission';

// 统一请求入口：createRequest / getData / ApiClient / ApiResponse / ApiError
export { createRequest, getData } from './api/request';
export type { ApiClient, CreateRequestOptions } from './api/request';
export type { ApiError, ApiResponse } from './api/types';
