import apiClient from './client';

interface ApiEnvelope<T> {
  success: boolean;
  code?: number;
  message?: string;
  data?: T;
}

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

function unwrap<T>(p: ApiEnvelope<T>): T {
  if (!p?.success) throw new Error(p?.message || 'Request failed');
  return p.data as T;
}

export async function listRoles(page = 1, pageSize = 100, targetTenantId?: string): Promise<RoleListResponse> {
  const r = await apiClient.get<ApiEnvelope<RoleListResponse>>('/api/v1/platform/roles', {
    params: {
      page,
      page_size: pageSize,
      ...(targetTenantId ? { target_tenant_id: targetTenantId } : {}),
    },
  });
  return unwrap(r.data);
}

export async function createRole(payload: { name: string; description?: string }): Promise<RoleItem> {
  const r = await apiClient.post<ApiEnvelope<RoleItem>>('/api/v1/platform/roles', payload);
  return unwrap(r.data);
}

export async function updateRole(id: number, payload: { name?: string; description?: string }): Promise<RoleItem> {
  const r = await apiClient.patch<ApiEnvelope<RoleItem>>(`/api/v1/platform/roles/${id}`, payload);
  return unwrap(r.data);
}

export async function deleteRole(id: number): Promise<void> {
  await apiClient.delete(`/api/v1/platform/roles/${id}`);
}

export async function listPermissions(): Promise<PermissionListResponse> {
  const r = await apiClient.get<ApiEnvelope<PermissionListResponse>>('/api/v1/platform/permissions', {
    params: { page_size: 200 },
  });
  return unwrap(r.data);
}

export async function getRolePermissions(roleId: number): Promise<number[]> {
  const r = await apiClient.get<ApiEnvelope<{ permission_ids: number[] }>>(
    `/api/v1/platform/roles/${roleId}/permissions`,
  );
  return unwrap(r.data).permission_ids;
}

export async function setRolePermissions(roleId: number, permissionIds: number[]): Promise<void> {
  await apiClient.put(`/api/v1/platform/roles/${roleId}/permissions`, { permission_ids: permissionIds });
}

export async function listRoleUsers(roleId: number): Promise<number[]> {
  const r = await apiClient.get<ApiEnvelope<{ user_ids: number[] }>>(`/api/v1/platform/roles/${roleId}/users`);
  return unwrap(r.data).user_ids;
}

export async function setRoleUsers(roleId: number, userIds: number[]): Promise<void> {
  await apiClient.put(`/api/v1/platform/roles/${roleId}/users`, { user_ids: userIds });
}
