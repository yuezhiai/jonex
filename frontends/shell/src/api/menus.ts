import { apiClient } from './auth';

export interface MenuNode {
  id: number;
  parent_id: number;
  name: string;
  path: string | null;
  icon: string | null;
  app_id: number | null;
  sort_order: number;
  permission_code: string | null;
  children?: MenuNode[];
}

/**
 * 获取当前用户可见菜单树（`GET /api/v1/platform/menus/my`）。
 * 后端已按用户权限码过滤（permission_code IS NULL 或持有该码的节点保留），
 * 父节点无可见子节点时被裁剪。
 */
export async function fetchMyMenus(): Promise<MenuNode[]> {
  const data = await apiClient.get<{ items: MenuNode[] }>('/platform/menus/my');
  return data?.items ?? [];
}
