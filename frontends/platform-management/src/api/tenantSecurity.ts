import apiClient from './request';

/**
 * 租户安全设置（租户管理员自助）取数入口。
 *
 * 后端契约（`/api/v1/platform/tenant-security-config`）与本仓库统一信封一致：
 * `{ success, code, data }`，`apiClient` 已在 @jonex/shared-lib 的 `unwrap` 里解包，
 * 所以这里直接写 data 的形状（与 `systemConfig.ts` 同一写法）。
 *
 * - GET  → `{ items }`，`items` 固定 4 项；
 *   `config_value` 是**当前生效值**（租户覆盖 → 平台默认 → 代码默认 依次回落），
 *   `platform_value` 是平台默认值，`is_overridden` 表示本租户是否设了自己的值；
 *   `min`/`max` 是该键的合法范围，**前端不要另写一份**（曾经前后端各写一份导致
 *   password_min_length 后端 72 / 前端 128 的漂移：用户能填 128，提交才 400）。
 *   类型标成可选是为了容忍老后端不下发的情况，页面那里有兜底 —— 现行后端一定会带上。
 * - PUT `/{config_key}`   → 写入本租户覆盖值，返回更新后的单个 item。
 * - DELETE `/{config_key}` → 删除本租户覆盖值（= 恢复平台默认），返回单个 item，
 *   `is_overridden` 变回 false。
 */

/** 权限码：读租户安全设置（tenant scope，与后端 `require_permission` 入参一致） */
export const TENANT_SECURITY_READ_PERMISSION = 'tenant:security:read';
/** 权限码：改租户安全设置（tenant scope） */
export const TENANT_SECURITY_WRITE_PERMISSION = 'tenant:security:write';

/** 后端固定的 4 个安全参数 */
export type TenantSecurityConfigKey =
  'session_timeout' | 'password_min_length' | 'login_lock_threshold' | 'lock_duration';

export interface TenantSecurityConfigItem {
  config_key: TenantSecurityConfigKey;
  /** 当前生效值（租户覆盖值 → 平台默认值 → 代码默认值 依次回落） */
  config_value: string;
  value_type: string;
  description: string | null;
  /** 平台默认值 */
  platform_value: string;
  /** 本租户是否设置了覆盖值 */
  is_overridden: boolean;
  /** 合法下界（后端下发，前端校验/输入框都用它，不要另写一份） */
  min?: number;
  /** 合法上界（后端下发） */
  max?: number;
}

export interface TenantSecurityConfigResponse {
  items: TenantSecurityConfigItem[];
}

/** 读取本租户 4 项安全设置（当前生效值 + 平台默认值 + 是否被覆盖） */
export async function getTenantSecurityConfig(): Promise<TenantSecurityConfigResponse> {
  return apiClient.get<TenantSecurityConfigResponse>('/platform/tenant-security-config');
}

/** 写入本租户对某个配置项的覆盖值，返回更新后的单项 */
export async function updateTenantSecurityConfig(
  configKey: TenantSecurityConfigKey,
  configValue: string,
  description?: string,
): Promise<TenantSecurityConfigItem> {
  // description 可选：不传就不带该键，避免用空串覆盖后端已有说明
  const body = description === undefined ? { config_value: configValue } : { config_value: configValue, description };
  return apiClient.put<TenantSecurityConfigItem>(
    `/platform/tenant-security-config/${encodeURIComponent(configKey)}`,
    body,
  );
}

/** 删除本租户的覆盖值 = 恢复平台默认，返回更新后的单项（is_overridden 变回 false） */
export async function resetTenantSecurityConfig(configKey: TenantSecurityConfigKey): Promise<TenantSecurityConfigItem> {
  return apiClient.delete<TenantSecurityConfigItem>(
    `/platform/tenant-security-config/${encodeURIComponent(configKey)}`,
  );
}
