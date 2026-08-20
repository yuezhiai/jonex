import apiClient from './client';

// ══ MCP Key 类型（来源 /api/v1/platform/mcp-keys）══

/** MCP Key 权限：view=仅查看、call=可调用、write=可写入 */
export type McpKeyPermission = 'view' | 'call' | 'write';

export interface McpKeyCreatePayload {
  name?: string;
  /** 用途描述 */
  note?: string | null;
  /** 领域空间 ID（必填，后端校验归属当前租户） */
  space_id: string;
  permissions?: McpKeyPermission[];
  allowed_kb_ids?: string[];
  /** 可访问的领域服务 ID 列表（由勾选服务派生） */
  service_ids?: string[];
  /** 各领域服务授权级别（call/view 两值） */
  service_permissions?: Array<{ service_id: string; permission_level: string }>;
  /** 有效期（ISO datetime；null = 永久有效） */
  expires_at?: string | null;
}

export interface McpKeyResetPayload {
  name?: string | null;
  /** 领域空间 ID（可选覆盖，重置后沿用旧值） */
  space_id?: string | null;
  permissions?: McpKeyPermission[] | null;
  allowed_kb_ids?: string[] | null;
  service_ids?: string[] | null;
  /** 有效期（ISO datetime；null = 永久有效） */
  expires_at?: string | null;
}

export interface McpKeyUpdatePayload {
  name?: string;
  /** 用途描述 */
  note?: string | null;
  /** 领域空间 ID（可选；创建时选定后编辑态只读） */
  space_id?: string;
  permissions?: McpKeyPermission[];
  allowed_kb_ids?: string[];
  service_ids?: string[];
  service_permissions?: Array<{ service_id: string; permission_level: string }>;
  /** 有效期（ISO datetime；null = 永久有效） */
  expires_at?: string | null;
}

export interface McpKeyItem {
  id: string;
  name: string;
  /** 用途描述 */
  note?: string | null;
  key_prefix: string;
  /** 领域空间 ID（历史 Key 可能为空） */
  space_id: string | null;
  permissions: McpKeyPermission[];
  /** 「领域范围」，前端不再展示/编辑（分期保留） */
  allowed_kb_ids: string[];
  /** 可访问的领域服务 ID 列表 */
  service_ids: string[];
  /** 各领域服务的授权级别（detail 返回：service_id + permission_level） */
  service_permissions?: Array<{ service_id: string; permission_level: string }>;
  /** 有效期（null = 永久有效） */
  expires_at?: string | null;
  /** 停用时间（非空 = 停用态） */
  disabled_at?: string | null;
  /** 4 态逻辑派生：active / disabled / expired / revoked */
  status?: string;
  created_by: string | null;
  created_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface McpKeyCreateResult {
  id: string;
  plaintext: string;
  name: string;
  key_prefix: string;
  space_id: string | null;
  permissions: string[];
  allowed_kb_ids: string[];
  service_ids: string[];
  created_at: string;
}

export interface McpKeyListResponse {
  items: McpKeyItem[];
  total: number;
}

// ══ 统一解包：兼容 { success } 与 { code } 两种响应信封 ══

interface ApiEnvelope<T> {
  success?: boolean;
  code?: number;
  message?: string;
  data?: T;
}

function unwrap<T>(payload: ApiEnvelope<T>): T {
  if (payload?.success === false || (typeof payload?.code === 'number' && payload.code !== 0)) {
    throw new Error(payload.message || 'Request failed');
  }
  return payload.data as T;
}

// ══ 接口方法（各自独立，不使用工厂函数）══

/** 全量列出当前租户下所有 MCP Key（含已撤销） */
export async function listMcpKeys(): Promise<McpKeyListResponse> {
  const resp = await apiClient.get<ApiEnvelope<McpKeyListResponse>>('/api/v1/platform/mcp-keys');
  return unwrap(resp.data);
}

/** 创建 MCP Key，返回值含一次性 plaintext */
export async function createMcpKey(data: McpKeyCreatePayload): Promise<McpKeyCreateResult> {
  const resp = await apiClient.post<ApiEnvelope<McpKeyCreateResult>>('/api/v1/platform/mcp-keys', data);
  return unwrap(resp.data);
}

/** 获取单个 MCP Key 详情 */
export async function getMcpKeyDetail(keyId: string): Promise<McpKeyItem> {
  const resp = await apiClient.get<ApiEnvelope<McpKeyItem>>(`/api/v1/platform/mcp-keys/${keyId}`);
  return unwrap(resp.data);
}

/** 更新（编辑）MCP Key 的 name/permissions/allowed_kb_ids/service_ids */
export async function updateMcpKey(keyId: string, data: McpKeyUpdatePayload): Promise<McpKeyItem> {
  const resp = await apiClient.put<ApiEnvelope<McpKeyItem>>(`/api/v1/platform/mcp-keys/${keyId}`, data);
  return unwrap(resp.data);
}

/** 删除 MCP Key（软删除——标记 Key 为已删除状态，不物理删除记录） */
export async function deleteMcpKey(keyId: string): Promise<void> {
  await apiClient.delete<ApiEnvelope<void>>(`/api/v1/platform/mcp-keys/${keyId}`);
}

/** 撤销 MCP Key（幂等操作，对已撤销的 key 再次调用不报错） */
export async function revokeMcpKey(keyId: string): Promise<void> {
  await apiClient.post<ApiEnvelope<void>>(`/api/v1/platform/mcp-keys/${keyId}/revoke`);
}

/** 重置 MCP Key，旧 Key 立即撤销并生成新 Key，返回新明文 */
export async function resetMcpKey(keyId: string, data?: McpKeyResetPayload): Promise<McpKeyCreateResult> {
  const resp = await apiClient.post<ApiEnvelope<McpKeyCreateResult>>(
    `/api/v1/platform/mcp-keys/${keyId}/reset`,
    data ?? {},
  );
  return unwrap(resp.data);
}
