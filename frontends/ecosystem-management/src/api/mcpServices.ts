import apiClient from './client';

// ══ MCP 服务目录类型（来源 /api/v1/platform/mcp-services，契约见 Phase 08 移交文档）══

export interface McpServiceItem {
  id: string; // 领域服务 ID
  name: string; // 服务名称
  description: string | null; // 服务描述
  domain_type: string | null; // 领域类型（如 knowledge_base）
  space_id: string; // 所属空间 ID
  space_name: string; // 所属空间名称
  status: string;
  kb_count: number; // 关联知识库数量
  kb_names: string[]; // 关联知识库名称列表
  is_published: boolean; // 是否已发布
  /** 服务本体是否可用（1=启用，0=停用），与 is_published 独立 */
  enabled: number;
  published_at: string | null; // 发布时间（ISO 8601）
  published_by: string | null; // 发布人
  last_call_at: string | null; // 最后调用时间
  created_at: string | null; // 创建时间
}

export interface McpServiceListResponse {
  items: McpServiceItem[];
  total: number;
}

export interface SyncResult {
  synced_count: number; // 本次同步新增记录数
}

export interface TestCallRequest {
  query: string; // 1-2000 字符
}

export interface TestCallResponse {
  answer: string; // 模拟回答，前缀 [模拟回答]
  kb_names: string[]; // 关联知识库名称
  relevance: number; // 相关度（0-1）
  latency_ms: number; // 模拟延迟（ms）
  request_id: string; // 请求追踪 ID
}

export interface AuthorizedKeyItem {
  key_id: string; // Key ID
  key_name: string; // Key 名称
  key_prefix: string; // Key 前缀（脱敏，形如 yxm_xxxx）
  permission_level: string; // 权限等级（'read' | '*'）
  org_name: string | null; // 所属组织名称
  key_status: string; // Key 状态（'active' | 'revoked'）
  org_id: string | null; // 组织 ID
}

export interface AuthorizedKeyListResponse {
  items: AuthorizedKeyItem[];
  total: number;
}

export interface ListMcpServicesParams {
  search?: string; // 模糊匹配服务名
  space_id?: string; // 按领域空间筛选
  status?: string; // 'published' | 'unpublished'，不传返回全部
}

// ══ 统一解包：兼容 { success }（部分服务）与 { code }（platform 标准信封）两种响应格式 ══

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

/** 领域服务列表（含发布状态、关联 KB 数量），三个筛选参数均可选 */
export async function listMcpServices(params: ListMcpServicesParams = {}): Promise<McpServiceListResponse> {
  const resp = await apiClient.get<ApiEnvelope<McpServiceListResponse>>('/api/v1/platform/mcp-services', {
    params: {
      search: params.search || undefined,
      space_id: params.space_id || undefined,
      status: params.status || undefined,
    },
  });
  return unwrapEnvelope(resp.data);
}

/** 手动从 knowledge_base 同步服务到发布表 */
export async function syncMcpServices(): Promise<SyncResult> {
  const resp = await apiClient.post<ApiEnvelope<SyncResult>>('/api/v1/platform/mcp-services/sync');
  return unwrapEnvelope(resp.data);
}

/** 发布领域服务（标记为 MCP 可见） */
export async function publishService(serviceId: string): Promise<McpServiceItem> {
  const resp = await apiClient.post<ApiEnvelope<McpServiceItem>>(
    `/api/v1/platform/mcp-services/${serviceId}/publish`,
  );
  return unwrapEnvelope(resp.data);
}

/** 取消发布领域服务 */
export async function unpublishService(serviceId: string): Promise<McpServiceItem> {
  const resp = await apiClient.post<ApiEnvelope<McpServiceItem>>(
    `/api/v1/platform/mcp-services/${serviceId}/unpublish`,
  );
  return unwrapEnvelope(resp.data);
}

/** 测试调用已发布的服务（一期返回模拟回答） */
export async function testCallService(serviceId: string, query: string): Promise<TestCallResponse> {
  const resp = await apiClient.post<ApiEnvelope<TestCallResponse>>(
    `/api/v1/platform/mcp-services/${serviceId}/test-call`,
    { query },
  );
  return unwrapEnvelope(resp.data);
}

/** 查看已授权 MCP Key 列表 */
export async function getAuthorizedKeys(serviceId: string): Promise<AuthorizedKeyListResponse> {
  const resp = await apiClient.get<ApiEnvelope<AuthorizedKeyListResponse>>(
    `/api/v1/platform/mcp-services/${serviceId}/authorized-keys`,
  );
  return unwrapEnvelope(resp.data);
}
