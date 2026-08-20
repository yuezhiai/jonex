import type { ShellUser } from './types';

/**
 * 当前用户是否为平台管理员。
 * 依据后端登录/me 响应计算的 is_platform_admin 布尔（持有 platform:admin 权限码）。
 */
export function isPlatformAdmin(user: ShellUser | null | undefined): boolean {
  return user?.isPlatformAdmin === true;
}
