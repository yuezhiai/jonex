import apiClient from './request';

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
  /** RBAC 角色绑定（目标租户内的角色 id 列表；创建后在同一事务内绑定） */
  role_ids?: number[];
}
export interface UserUpdatePayload {
  display_name?: string;
  email?: string;
  role?: string;
  status?: number;
  /** [jonex] 可选改密：非空时后端重哈希写入 password_hash；留空/不传则不改密码 */
  new_password?: string;
}

export async function listAllUsers(): Promise<UserListResponse> {
  return apiClient.get<UserListResponse>('/platform/users/all');
}

export async function listUsers(page = 1, pageSize = 100): Promise<UserListResponse> {
  return apiClient.get<UserListResponse>('/platform/users', {
    params: { page, page_size: pageSize },
  });
}

/**
 * 拉取租户内全部用户（后端单页上限 500）。
 *
 * 角色分配弹窗需要「全部用户」才能把已绑定 id 映射成姓名；只取第一页时，排在
 * 后面页的合法用户会因匹配不到 option 而被渲染成裸数字。
 */
export async function listAllTenantUsers(): Promise<UserListResponse> {
  const pageSize = 500;
  const items: UserItem[] = [];
  let total: number;
  let page = 1;
  do {
    const res = await listUsers(page, pageSize);
    items.push(...res.items);
    total = res.total;
    if (res.items.length === 0) break;
    page += 1;
  } while (items.length < total);
  return { items, total: items.length };
}

export async function createUser(data: UserCreatePayload): Promise<UserItem> {
  return apiClient.post<UserItem>('/platform/users', data);
}

export async function updateUser(id: number, data: UserUpdatePayload, targetTenantId?: string): Promise<UserItem> {
  return apiClient.patch<UserItem>(`/platform/users/${id}`, {
    ...data,
    ...(targetTenantId ? { target_tenant_id: targetTenantId } : {}),
  });
}

export async function deleteUser(id: number, targetTenantId?: string): Promise<void> {
  // 注意 ApiClient 的签名差异：delete<T>(url, data?, config?) —— 第二个参数是「请求体」，
  // 第三个才是 axios config；而 get<T>(url, config?) 的第二个参数就是 config。
  // 想传 query 必须占位到第三个参数，否则 { params } 会被当成 body 发出去，
  // 后端的 Query(target_tenant_id) 收不到，跨租户删除照旧失败。
  await apiClient.delete<null>(
    `/platform/users/${id}`,
    undefined,
    targetTenantId ? { params: { target_tenant_id: targetTenantId } } : undefined,
  );
}

/** 用户当前绑定的角色 id 列表（RBAC 多角色） */
export async function getUserRoles(userId: number, targetTenantId?: string): Promise<number[]> {
  const data = await apiClient.get<{ role_ids: number[] }>(`/platform/users/${userId}/roles`, {
    params: targetTenantId ? { target_tenant_id: targetTenantId } : undefined,
  });
  return data.role_ids;
}

/** 更新用户角色绑定（delete-then-insert） */
export async function setUserRoles(userId: number, roleIds: number[], targetTenantId?: string): Promise<void> {
  await apiClient.put<null>(`/platform/users/${userId}/roles`, {
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
