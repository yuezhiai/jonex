import apiClient from './request';

export interface RoleItem {
  id: number;
  tenant_id: string;
  name: string;
  description: string | null;
  is_system: number;
  created_at: string | null;
}

export interface PermissionItem {
  id: number;
  code: string;
  name: string;
  resource: string;
  action: string;
  description: string | null;
}

export interface RoleListResponse {
  total: number;
  items: RoleItem[];
}
export interface PermissionListResponse {
  total: number;
  items: PermissionItem[];
}

export async function listRoles(page = 1, pageSize = 100, targetTenantId?: string): Promise<RoleListResponse> {
  return apiClient.get<RoleListResponse>('/platform/roles', {
    params: {
      page,
      page_size: pageSize,
      ...(targetTenantId ? { target_tenant_id: targetTenantId } : {}),
    },
  });
}

/**
 * 拉取租户全部角色（后端单页上限 100）。
 *
 * 用户编辑弹窗要用角色名映射已绑定的角色 id；只取第一页时，第二页之后的合法
 * 角色会被渲染成裸数字（保存时 ID 仍有效，但界面不可读）。
 */
export async function listAllRoles(targetTenantId?: string): Promise<RoleListResponse> {
  const pageSize = 100;
  const items: RoleItem[] = [];
  let total: number;
  let page = 1;
  do {
    const res = await listRoles(page, pageSize, targetTenantId);
    items.push(...res.items);
    total = res.total;
    if (res.items.length === 0) break;
    page += 1;
  } while (items.length < total);
  return { items, total: items.length };
}

export async function createRole(payload: { name: string; description?: string }): Promise<RoleItem> {
  return apiClient.post<RoleItem>('/platform/roles', payload);
}

export async function updateRole(id: number, payload: { name?: string; description?: string }): Promise<RoleItem> {
  return apiClient.patch<RoleItem>(`/platform/roles/${id}`, payload);
}

export async function deleteRole(id: number): Promise<void> {
  await apiClient.delete<null>(`/platform/roles/${id}`);
}

export async function listPermissions(): Promise<PermissionListResponse> {
  return apiClient.get<PermissionListResponse>('/platform/permissions', {
    params: { page_size: 200 },
  });
}

export async function getRolePermissions(roleId: number): Promise<number[]> {
  const data = await apiClient.get<{ permission_ids: number[] }>(`/platform/roles/${roleId}/permissions`);
  return data.permission_ids;
}

export async function setRolePermissions(roleId: number, permissionIds: number[]): Promise<void> {
  await apiClient.put<null>(`/platform/roles/${roleId}/permissions`, { permission_ids: permissionIds });
}

export async function listRoleUsers(roleId: number): Promise<number[]> {
  const data = await apiClient.get<{ user_ids: number[] }>(`/platform/roles/${roleId}/users`);
  return data.user_ids;
}

export async function setRoleUsers(roleId: number, userIds: number[]): Promise<void> {
  await apiClient.put<null>(`/platform/roles/${roleId}/users`, { user_ids: userIds });
}
