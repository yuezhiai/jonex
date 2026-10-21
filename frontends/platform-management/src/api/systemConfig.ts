import apiClient from './request';

/**
 * 权限码：读平台全局系统配置（platform scope，与后端 `require_permission` 入参一致）。
 *
 * 「系统配置」页现在同时承载平台与租户两套作用域，两个码都要在这里声明：
 * 路由可见性是 `[platform:config:read, tenant:security:read]`（任一命中），
 * 页面内部再按码分段渲染，所以两个码都不能只写死在路由配置里。
 */
export const PLATFORM_CONFIG_READ_PERMISSION = 'platform:config:read';
/** 权限码：写平台全局系统配置（platform scope） */
export const PLATFORM_CONFIG_WRITE_PERMISSION = 'platform:config:write';

export interface SystemConfigItem {
  id: number;
  config_group: string;
  config_key: string;
  config_value: string | null;
  value_type: string;
  description: string | null;
}

export async function listSystemConfigs(): Promise<{ items: SystemConfigItem[] }> {
  return apiClient.get<{ items: SystemConfigItem[] }>('/platform/system-configs');
}

export async function updateSystemConfig(key: string, value: string): Promise<SystemConfigItem> {
  return apiClient.put<SystemConfigItem>(`/platform/system-configs/${key}`, {
    config_value: value,
  });
}
