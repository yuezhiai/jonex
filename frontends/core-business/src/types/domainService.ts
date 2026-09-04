/** 领域服务（领域服务管理）类型定义 */

export interface DomainServiceItem {
  id: string;
  name: string;
  description: string | null;
  space_id: string;
  /** 所属空间名称，后端直接返回 */
  space_name?: string;
  domain_type: string | null;
  status: string;
  api_key_encrypted: string | null;
  /** 关联的知识库 ID 列表 */
  kb_ids: string[];
  /** 关联的知识库名称列表，与 kb_ids 一一对应 */
  kb_names?: string[];
  created_at: string | null;
  updated_at: string | null;
}

export interface DomainServiceListResult {
  items: DomainServiceItem[];
  total: number;
  offset: number;
  limit: number;
}

export interface DomainServiceFormData {
  name: string;
  space_id: string;
  description?: string;
  /** 服务类型 */
  domain_type?: string;
  /** 关联知识库 ID 列表 */
  kb_ids?: string[];
  status?: string;
}

export interface DomainServiceStatusOption {
  value: string;
  label: string;
  color: string;
}

export function getServiceStatusMap(t: (key: string) => string): Record<string, DomainServiceStatusOption> {
  return {
    active: { value: 'active', label: t('status.active'), color: '#059669' },
    inactive: { value: 'inactive', label: t('status.inactive'), color: '#dc2626' },
    testing: { value: 'testing', label: t('domainService.status.testing'), color: '#d97706' },
  };
}

/** 知识库 - 用于创建/编辑弹窗的勾选项 */
export interface KnowledgeBaseOption {
  id: string;
  name: string;
}

/**
 * 空间资源身份取值（两级）。
 *
 * [jonex] 权限重构 B2（D2）：原为 `'viewer' | 'manager'`，且存在第三态 owner
 * （不落 space_permissions、只认 spaces.owner_id）。D2 取消 owner 概念：
 * 创建人以 `space_manager` 身份正常落表，`owner_id` 退化为纯展示字段。
 *
 * 必须与后端 space_permission_service.SPACE_MANAGER / SPACE_MEMBER 保持一致 ——
 * 后端白名单会拒绝旧取值（400），前后端不同步会导致空间设置页保存失败。
 * 方案 docs/permissions/PERMISSIONS_REDESIGN.md §3.3
 */
export type SpaceRole = 'space_manager' | 'member';

/** 权限成员 */
export interface PermMember {
  /** user_id 来自平台用户表 */
  id: string;
  /** 展示名称：display_name > username */
  name: string;
  /** 部门/角色信息，用于副标题展示 */
  department: string;
  avatar: string;
  avatarColor: string;
  role: SpaceRole;
}

/** 简单哈希色值 */
export function userNameToColor(name: string): string {
  const colors = ['#3b82f6', '#10b981', '#8b5cf6', '#f97316', '#ec4899', '#06b6d4', '#84cc16', '#f43f5e'];
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return colors[Math.abs(hash) % colors.length];
}

/** 将平台用户转为 PermMember */
export function userToPermMember(
  user: { id: number | string; username: string; display_name?: string | null; role?: string },
  permRole: SpaceRole = 'member',
): PermMember {
  const name = (user.display_name || user.username || String(user.id)).trim();
  return {
    id: String(user.id),
    name,
    department: user.role || '',
    avatar: name.charAt(0).toUpperCase(),
    avatarColor: userNameToColor(name),
    role: permRole,
  };
}

/** 添加成员候选用户（后端 permission-candidates 统一返回结构） */
export interface MemberCandidate {
  user_id: string;
  username: string | null;
  display_name: string | null;
  email: string | null;
}

/** 将候选用户转为 PermMember（候选结构无 id/role 字段，与 userToPermMember 区分） */
export function candidateToPermMember(
  candidate: MemberCandidate,
  permRole: SpaceRole = 'member',
): PermMember {
  const name = (candidate.display_name || candidate.username || candidate.user_id).trim();
  return {
    id: candidate.user_id,
    name,
    department: candidate.email || '',
    avatar: name.charAt(0).toUpperCase(),
    avatarColor: userNameToColor(name),
    role: permRole,
  };
}

/** API Key 条目 */
export interface ServiceApiKeyItem {
  id: string;
  service_id: string;
  key_prefix: string;
  key_encrypted: string;
  expires_at: string | null;
  /** 是否启用：后端 SmallInteger 透出 0/1（1=启用） */
  is_active: number;
  created_at: string | null;
}

/** 原型中的知识库列表（后端暂无独立列表接口） */
export const MOCK_KNOWLEDGE_BASES: KnowledgeBaseOption[] = [
  { id: 'kb-finance', name: 'Financial Products KB' },
  { id: 'kb-medical', name: 'Medical Literature KB' },
  { id: 'kb-manufacturing', name: 'Equipment Fault KB' },
  { id: 'kb-education', name: 'Course Resources KB' },
  { id: 'kb-legal', name: 'Laws & Regulations KB' },
  { id: 'kb-customer', name: 'Customer Service KB' },
  { id: 'kb-compliance', name: 'Compliance Documents KB' },
  { id: 'kb-quality', name: 'Quality Inspection KB' },
];
