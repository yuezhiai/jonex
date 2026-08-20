import apiClient from './client';

interface ApiEnvelope<T> {
  success: boolean;
  code?: number;
  message?: string;
  data?: T;
}

export interface UserItem {
  id: number;
  tenant_id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  role: string;
  /** RBAC 绑定角色名（与编辑弹窗 get_roles 同源）；users.role 是历史列仅作兜底 */
  role_names?: string[];
  status: number;
  last_login_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface UserListResponse {
  items: UserItem[];
  total: number;
}

export interface UserCreatePayload {
  username: string;
  password: string;
  display_name?: string;
  email?: string;
  role?: string;
  target_tenant_id?: string;
  /** RBAC 角色绑定（目标租户内的角色 id；创建后绑定） */
  role_id?: number;
}
export interface UserUpdatePayload {
  display_name?: string;
  email?: string;
  role?: string;
  status?: number;
}

function unwrap<T>(p: ApiEnvelope<T>): T {
  if (!p?.success) throw new Error(p?.message || 'Request failed');
  return p.data as T;
}

export async function listAllUsers(): Promise<UserListResponse> {
  const r = await apiClient.get<ApiEnvelope<UserListResponse>>('/api/v1/platform/users/all');
  return unwrap(r.data);
}

export async function listUsers(page = 1, pageSize = 100): Promise<UserListResponse> {
  const r = await apiClient.get<ApiEnvelope<UserListResponse>>('/api/v1/platform/users', {
    params: { page, page_size: pageSize },
  });
  return unwrap(r.data);
}

export async function createUser(data: UserCreatePayload): Promise<UserItem> {
  const r = await apiClient.post<ApiEnvelope<UserItem>>('/api/v1/platform/users', data);
  return unwrap(r.data);
}

export async function updateUser(id: number, data: UserUpdatePayload, targetTenantId?: string): Promise<UserItem> {
  const r = await apiClient.patch<ApiEnvelope<UserItem>>(`/api/v1/platform/users/${id}`, {
    ...data,
    ...(targetTenantId ? { target_tenant_id: targetTenantId } : {}),
  });
  return unwrap(r.data);
}

export async function deleteUser(id: number): Promise<void> {
  await apiClient.delete(`/api/v1/platform/users/${id}`);
}

/** 用户当前绑定的角色 id 列表（RBAC 多角色） */
export async function getUserRoles(userId: number, targetTenantId?: string): Promise<number[]> {
  const r = await apiClient.get<ApiEnvelope<{ role_ids: number[] }>>(`/api/v1/platform/users/${userId}/roles`, {
    params: targetTenantId ? { target_tenant_id: targetTenantId } : undefined,
  });
  return unwrap(r.data).role_ids;
}

/** 更新用户角色绑定（delete-then-insert） */
export async function setUserRoles(userId: number, roleIds: number[], targetTenantId?: string): Promise<void> {
  await apiClient.put(`/api/v1/platform/users/${userId}/roles`, {
    role_ids: roleIds,
    ...(targetTenantId ? { target_tenant_id: targetTenantId } : {}),
  });
}

export function getRoleLabel(t: (key: string) => string, role: string): string {
  const m: Record<string, string> = { admin: t('users.roleAdmin'), user: t('users.roleUser') };
  return m[role] || role;
}

export function getUserStatus(t: (key: string) => string, s: number): { label: string; color: string } {
  return s === 1 ? { label: t('status.enabled'), color: 'success' } : { label: t('status.disabled'), color: 'error' };
}
