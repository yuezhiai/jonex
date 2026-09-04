import { request, getData } from './request';

/** 平台用户（对应后端 UserResponse） */
export interface PlatformUser {
  id: number;
  tenant_id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  role: string;
  status: number;
  last_login_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** RBAC 绑定角色名（users.role 是历史列仅兜底；role_names 为真实绑定角色） */
  role_names?: string[];
  /** RBAC 绑定角色 id（与 role_names 同源；前端按管理员角色 id 过滤可选成员） */
  role_ids?: number[];
}

export interface PlatformUserListResult {
  total: number;
  items: PlatformUser[];
}

/** 获取平台用户分页列表（按租户隔离） */
export async function listUsers(page = 1, pageSize = 500): Promise<PlatformUserListResult> {
  return getData<PlatformUserListResult>(request.get('/platform/users', { params: { page, page_size: pageSize } }));
}
