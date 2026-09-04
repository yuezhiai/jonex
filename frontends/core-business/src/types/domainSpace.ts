import type { SpaceRole } from './domainService';

export interface DomainSpace {
  id: string;
  name: string;
  description: string | null;
  owner_id: string | null;
  status: 'active' | 'inactive' | 'disabled';
  knowledge_base_count: number;
  service_count: number;
  created_at: string | null;
  updated_at: string | null;
  /** 后端按 owner/service:write 计算（空间级）——modal 从列表页打开，列表项必须带该字段 */
  can_manage_permissions: boolean;
  /** 空间写权限（KB/文档/服务写）：owner 或 manager 成员或租户管理员 */
  can_write_space: boolean;
}

/** 空间权限记录（对应后端 space_permissions 表） */
export interface SpacePermission {
  id: string;
  user_id: string;
  /**
   * [jonex] 权限重构 B2（D2）：`'viewer' | 'manager'` → `SpaceRole`
   * （`'space_manager' | 'member'`）。取值定义在 types/domainService.ts，
   * 必须与后端 space_permission_service 的常量一致。
   */
  role: SpaceRole;
  created_at: string | null;
  /** 后端 join platform.users 返回；普通成员无 user:read，前端不再拉 /users 拼名字 */
  display_name: string | null;
}

/** 空间成员 UI 展示模型 */
export interface SpaceMember {
  id: string;
  name: string;
  avatar: string;
  department: string;
  avatarColor: string;
  role: SpaceRole;
}

export interface DomainSpaceListParams {
  offset?: number;
  limit?: number;
  keyword?: string;
}

export interface DomainSpaceListResult {
  items: DomainSpace[];
  total: number;
  offset: number;
  limit: number;
  /** 租户级：当前用户能否创建空间（持有 service:write） */
  can_create_space: boolean;
}

export interface DomainSpaceFormData {
  name: string;
  description?: string;
  owner_id?: string;
  status?: 'active' | 'inactive' | 'disabled';
}

export function getSpaceStatusMap(t: (key: string) => string): Record<string, { label: string; color: string }> {
  return {
    active: { label: t('status.active'), color: 'green' },
    inactive: { label: t('domainSpace.maintenanceStatus'), color: 'orange' },
    disabled: { label: t('status.disabled'), color: 'red' },
  };
}
