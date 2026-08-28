import apiClient from './request';

// ══ 服务视角 Key 管理 + 启用/停用 类型（来源 /api/v1/platform/mcp-services/{id}/keys 与 /api/v1/knowledge-base/services/{id}/enable|disable）══

/** 服务级权限级别：view/call/write/* 依次增强 */
export type ServicePermissionLevel = 'view' | 'call' | 'write' | '*';

export interface AddKeyRequest {
  key_id: string; // 已有 MCP Key ID
  permission_level?: ServicePermissionLevel; // 默认 "call"
}

export interface AddKeyResponse {
  key_id: string;
  service_id: string;
  permission_level: string;
}

export interface CreateKeyForServiceRequest {
  name: string; // Key 名称（必填，trim 后非空）
  permission_level?: ServicePermissionLevel; // 默认 "call"
  description?: string | null; // 描述
  expires_at?: string | null; // 过期时间（ISO datetime）
}

export interface CreateKeyForServiceResponse {
  id: string; // Key ID
  plaintext: string; // ⚠️ 一次性明文 Key（yxm_xxx...），仅此时返回
  name: string;
  key_prefix: string; // 前缀（yxm_ 后前 8 位）
  permission_level: string;
  service_id: string;
  created_at: string; // UTC datetime
  expires_at: string | null;
}

export interface UpdatePermissionRequest {
  permission_level: ServicePermissionLevel; // 必填
}

export interface UpdatePermissionResponse {
  key_id: string;
  service_id: string;
  permission_level: string;
}

export interface RemoveKeyResponse {
  removed: true;
}

export interface DomainServiceResponse {
  id: string;
  tenant_id: string;
  space_id: string;
  name: string;
  description: string | null;
  domain_type: string | null;
  status: string;
  api_key_encrypted: string | null;
  enabled: number; // 1 = 已启用，0 = 未启用
  kb_ids: string[];
  space_name: string;
  kb_names: string[];
  created_at: string | null;
  updated_at: string | null;
}

// ══ API（apiClient.get<T> 已解包，返回 data）══

/** 添加已有 Key 到服务授权 */
export async function addKeyToService(serviceId: string, data: AddKeyRequest): Promise<AddKeyResponse> {
  return apiClient.post<AddKeyResponse>(`/platform/mcp-services/${serviceId}/keys`, data);
}

/** 创建新 Key + 自动关联到服务（返回一次性明文，仅此时可获取） */
export async function createKeyForService(
  serviceId: string,
  data: CreateKeyForServiceRequest,
): Promise<CreateKeyForServiceResponse> {
  return apiClient.post<CreateKeyForServiceResponse>(`/platform/mcp-services/${serviceId}/keys/new`, data);
}

/** 切换服务授权 Key 的权限（view → call → write → * 依序循环） */
export async function updateKeyPermission(
  serviceId: string,
  keyId: string,
  data: UpdatePermissionRequest,
): Promise<UpdatePermissionResponse> {
  return apiClient.patch<UpdatePermissionResponse>(`/platform/mcp-services/${serviceId}/keys/${keyId}`, data);
}

/** 移除单服务授权（仅移除映射，不删除/撤销 Key 本身） */
export async function removeKeyFromService(serviceId: string, keyId: string): Promise<RemoveKeyResponse> {
  return apiClient.delete<RemoveKeyResponse>(`/platform/mcp-services/${serviceId}/keys/${keyId}`);
}

/** 启用领域服务 */
export async function enableDomainService(serviceId: string): Promise<DomainServiceResponse> {
  return apiClient.post<DomainServiceResponse>(`/knowledge-base/services/${serviceId}/enable`);
}

/** 停用领域服务 */
export async function disableDomainService(serviceId: string): Promise<DomainServiceResponse> {
  return apiClient.post<DomainServiceResponse>(`/knowledge-base/services/${serviceId}/disable`);
}
