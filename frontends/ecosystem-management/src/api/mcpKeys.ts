import apiClient from './request';

// ══ MCP Key 类型（来源 /api/v1/platform/mcp-keys，统一 Key：服务授权 + 知识写入）══

/** 服务级授权级别：view=仅查看、call=可调用（Phase 16 收敛为 call/view） */
export type McpServicePermissionLevel = 'view' | 'call';

/** 单个知识库写入授权（grants[] 列表项） */
export interface WriteGrant {
  /** 知识库 ID（uuid） */
  kb: string;
  /** "all"=全目录可写（directories 必须为空）| "specified"=仅指定目录可写（directories 必须非空） */
  mode: 'all' | 'specified';
  /** 平铺 folder_id 列表；仅 mode=specified 时非空 */
  directories: string[];
}

/** 单条服务授权映射（create/update 提交） */
export interface ServicePermissionInput {
  service_id: string;
  permission_level: McpServicePermissionLevel;
}

export interface McpKeyCreatePayload {
  name?: string;
  /** 用途描述 */
  note?: string | null;
  /** 领域空间 ID（必填，后端校验归属当前租户） */
  space_id: string;
  /** 各领域服务授权级别（call/view） */
  service_permissions?: ServicePermissionInput[];
  /** 幂等键（必填；后端按 client_request_id 去重，命中返回脱敏信息 + delivery_failed=true） */
  client_request_id: string;
  /** 有效期（ISO datetime；null = 永久有效） */
  expires_at?: string | null;
  /** 知识写入授权（可选；null/不传 = 不开写入；非空数组 = 开启写入，空数组会报错） */
  write_grants?: WriteGrant[] | null;
}

export interface McpKeyUpdatePayload {
  name?: string;
  /** 用途描述 */
  note?: string | null;
  /** 领域空间 ID（可选；创建后编辑态只读） */
  space_id?: string;
  service_permissions?: ServicePermissionInput[];
  /** 有效期（ISO datetime；null = 永久有效） */
  expires_at?: string | null;
  /** 知识写入授权（可选；不传 = 保留原值；null = 关闭写入；非空数组 = 覆盖，空数组会报错） */
  write_grants?: WriteGrant[] | null;
}

export interface McpKeyRecreatePayload {
  /** 覆盖新 Key 有效期（不传默认 12 个月） */
  expires_at?: string | null;
  /** 覆盖新 Key 名称（可选） */
  name?: string;
}

export interface McpKeyItem {
  id: string;
  name: string;
  /** 用途描述 */
  note?: string | null;
  key_prefix: string;
  /** 领域空间 ID（历史 Key 可能为空） */
  space_id: string | null;
  /** 各领域服务的授权级别（detail/list 返回：service_id + permission_level） */
  service_permissions?: Array<{ service_id: string; permission_level: string }>;
  /** 知识写入授权（统一 Key；空数组 = 纯服务访问 Key） */
  write_grants?: WriteGrant[];
  /** 授权摘要（可调用 N · 仅查看 N · 写入开关） */
  auth_summary?: string;
  /** 写入范围摘要（可写知识库 N · 指定目录 N） */
  write_scope_summary?: string;
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
  /** 一次性明文（仅创建/重新创建响应返回；幂等命中为 null） */
  plaintext: string | null;
  name: string;
  key_prefix: string;
  space_id: string | null;
  service_permissions?: Array<{ service_id: string; permission_level: string }>;
  write_grants?: WriteGrant[];
  status: string;
  auth_summary?: string;
  write_scope_summary?: string;
  /** WorkBuddy 兼容连接配置（幂等命中为 null） */
  mcp_config?: Record<string, unknown> | null;
  /** 幂等命中：true（返回脱敏信息，无明文/配置） */
  delivery_failed?: boolean;
  /** 创建时因未发布/已停用被丢弃的服务 ID */
  dropped_service_ids?: string[];
  created_at: string;
  expires_at?: string | null;
}

export interface McpKeyListResponse {
  items: McpKeyItem[];
  total: number;
}

// ══ 接口方法（各自独立，apiClient.get<T> 已解包，返回 data）══

/** 全量列出当前租户下所有 MCP Key（含已撤销） */
export async function listMcpKeys(): Promise<McpKeyListResponse> {
  return apiClient.get<McpKeyListResponse>('/platform/mcp-keys');
}

/** 创建 MCP Key，返回值含一次性 plaintext（幂等命中时 plaintext=null + delivery_failed=true） */
export async function createMcpKey(data: McpKeyCreatePayload): Promise<McpKeyCreateResult> {
  return apiClient.post<McpKeyCreateResult>('/platform/mcp-keys', data);
}

/** 获取单个 MCP Key 详情 */
export async function getMcpKeyDetail(keyId: string): Promise<McpKeyItem> {
  return apiClient.get<McpKeyItem>(`/platform/mcp-keys/${keyId}`);
}

/** 更新（编辑）MCP Key 的 name/note/service_permissions/write_grants/expires_at */
export async function updateMcpKey(keyId: string, data: McpKeyUpdatePayload): Promise<McpKeyItem> {
  return apiClient.put<McpKeyItem>(`/platform/mcp-keys/${keyId}`, data);
}

/** 删除 MCP Key（软删除——标记 Key 为已删除状态，不物理删除记录） */
export async function deleteMcpKey(keyId: string): Promise<void> {
  await apiClient.delete<null>(`/platform/mcp-keys/${keyId}`);
}

/** 撤销 MCP Key（幂等操作，对已撤销的 key 再次调用不报错） */
export async function revokeMcpKey(keyId: string): Promise<void> {
  await apiClient.post<null>(`/platform/mcp-keys/${keyId}/revoke`);
}

/** 停用/启用 MCP Key（可逆，key 不变；已撤销 Key 调 toggle → 409） */
export async function toggleMcpKey(keyId: string): Promise<McpKeyItem> {
  return apiClient.post<McpKeyItem>(`/platform/mcp-keys/${keyId}/toggle`);
}

/** 重新创建 MCP Key（仅过期态可用）：继承原授权 + 生成新明文，旧 Key 保留历史 */
export async function recreateMcpKey(keyId: string, data?: McpKeyRecreatePayload): Promise<McpKeyCreateResult> {
  return apiClient.post<McpKeyCreateResult>(`/platform/mcp-keys/${keyId}/recreate`, data ?? {});
}
