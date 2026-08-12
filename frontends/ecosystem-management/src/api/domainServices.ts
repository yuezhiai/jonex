import apiClient from './client';

// ══ 服务视角 Key 管理 + 启用/停用 类型（来源 /api/v1/platform/mcp-services/{id}/keys 与 /api/v1/knowledge-base/services/{id}/enable|disable）══

export interface AddKeyRequest {
  key_id: string; // 已有 MCP Key ID
  permission_level?: 'call' | 'view'; // 默认 "call"
}

export interface AddKeyResponse {
  key_id: string;
  service_id: string;
  permission_level: string;
}

export interface CreateKeyForServiceRequest {
  name: string; // Key 名称（必填，trim 后非空）
  permission_level?: 'call' | 'view'; // 默认 "call"
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
  permission_level: 'call' | 'view'; // 必填
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

// ══ 统一解包：与 mcpServices.ts 一致，兼容 { success } 与 { code } 双信封 ══

interface ApiEnvelope<T> {
  success?: boolean;
  code?: number;
  message?: string;
  data?: T;
}

function unwrapEnvelope<T>(payload: ApiEnvelope<T>): T {
  if (payload?.success === false || (typeof payload?.code === 'number' && payload.code !== 0)) {
    throw new Error(payload?.message || 'Request failed');
  }
  return payload.data as T;
}

// ══ API ══

/** 添加已有 Key 到服务授权 */
export async function addKeyToService(serviceId: string, data: AddKeyRequest): Promise<AddKeyResponse> {
  const resp = await apiClient.post<ApiEnvelope<AddKeyResponse>>(
    `/api/v1/platform/mcp-services/${serviceId}/keys`,
    data,
  );
  return unwrapEnvelope(resp.data);
}

/** 创建新 Key + 自动关联到服务（返回一次性明文，仅此时可获取） */
export async function createKeyForService(
  serviceId: string,
  data: CreateKeyForServiceRequest,
): Promise<CreateKeyForServiceResponse> {
  const resp = await apiClient.post<ApiEnvelope<CreateKeyForServiceResponse>>(
    `/api/v1/platform/mcp-services/${serviceId}/keys/new`,
    data,
  );
  return unwrapEnvelope(resp.data);
}

/** 切换服务授权 Key 的权限（call ↔ view） */
export async function updateKeyPermission(
  serviceId: string,
  keyId: string,
  data: UpdatePermissionRequest,
): Promise<UpdatePermissionResponse> {
  const resp = await apiClient.patch<ApiEnvelope<UpdatePermissionResponse>>(
    `/api/v1/platform/mcp-services/${serviceId}/keys/${keyId}`,
    data,
  );
  return unwrapEnvelope(resp.data);
}

/** 移除单服务授权（仅移除映射，不删除/撤销 Key 本身） */
export async function removeKeyFromService(serviceId: string, keyId: string): Promise<RemoveKeyResponse> {
  const resp = await apiClient.delete<ApiEnvelope<RemoveKeyResponse>>(
    `/api/v1/platform/mcp-services/${serviceId}/keys/${keyId}`,
  );
  return unwrapEnvelope(resp.data);
}

/** 启用领域服务 */
export async function enableDomainService(serviceId: string): Promise<DomainServiceResponse> {
  const resp = await apiClient.post<ApiEnvelope<DomainServiceResponse>>(
    `/api/v1/knowledge-base/services/${serviceId}/enable`,
  );
  return unwrapEnvelope(resp.data);
}

/** 停用领域服务 */
export async function disableDomainService(serviceId: string): Promise<DomainServiceResponse> {
  const resp = await apiClient.post<ApiEnvelope<DomainServiceResponse>>(
    `/api/v1/knowledge-base/services/${serviceId}/disable`,
  );
  return unwrapEnvelope(resp.data);
}
