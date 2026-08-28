import apiClient from './request';

// ══ MCP 服务目录类型（来源 /api/v1/platform/mcp-services，契约见 Phase 08 移交文档）══

export interface McpServiceItem {
  id: string; // 领域服务 ID（系统服务为固定合成 ID system.knowledge_document_write）
  name: string; // 服务名称
  /** MCP Tool 名称（后端统一字段）。领域服务可编辑（PUT /tool 保存），系统服务内置固定不可改 */
  tool?: string | null;
  /** MCP Tool 描述 */
  tool_description?: string | null;
  /** 'domain'（领域服务）| 'system'（系统内置服务，如 knowledge_document_write） */
  service_type?: string;
  description: string | null; // 服务描述
  domain_type: string | null; // 领域类型（如 knowledge_base）
  space_id: string; // 所属空间 ID（系统服务为空串）
  space_name: string; // 所属空间名称（系统服务为空串）
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

/** API 接入信息（领域服务标准 REST 出口 PRD K5；endpoint 为规划路径占位，接口本次未实现） */
export interface ApiAccessInfo {
  endpoint: string;
  method: string;
  /** 认证方式：固定 'api-key'（领域服务 API Key） */
  auth: string;
  sample_payload: Record<string, unknown>;
}

/** MCP 接入信息（transport 固定 streamable-http，auth_scheme 固定 bearer，server_url 由后端下发） */
export interface McpAccessInfo {
  transport: string;
  tool_name: string | null;
  auth_scheme: string;
  /** MCP 服务地址（详情接口 access.mcp.server_url，取自后端 MCP_SERVER_PUBLIC_URL 配置；未配置为空） */
  server_url?: string | null;
}

/** 服务接入信息（详情响应 access 字段；系统写服务 access.api = null） */
export interface ServiceAccessInfo {
  api: ApiAccessInfo | null;
  mcp: McpAccessInfo | null;
}

/** MCP 服务详情（详情接口在列表项基础上补充 kb_ids / updated_at / access） */
export interface McpServiceDetail extends McpServiceItem {
  kb_ids?: string[];
  updated_at?: string | null;
  access?: ServiceAccessInfo | null;
}

export interface McpServiceListResponse {
  items: McpServiceItem[];
  total: number;
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
  key_prefix: string; // Key 前缀（脱敏）
  permission_level: string; // 服务级权限等级（'call' | 'view'）
  key_status: string; // Key 状态（'active' | 'revoked' 等）
  /** TODO: 待确认点 #1 —— 后端 AuthorizedKeyResponse 是否透出 expires_at（服务侧授权表有效期列） */
  expires_at?: string | null;
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

// ══ API（apiClient.get<T> 已解包，返回 data）══

/** 获取 MCP 服务详情（含 API / MCP 接入信息 access） */
export async function getMcpServiceDetail(serviceId: string): Promise<McpServiceDetail> {
  return apiClient.get<McpServiceDetail>(`/platform/mcp-services/${serviceId}`);
}

/** 领域服务列表（含发布状态、关联 KB 数量），三个筛选参数均可选 */
export async function listMcpServices(params: ListMcpServicesParams = {}): Promise<McpServiceListResponse> {
  return apiClient.get<McpServiceListResponse>('/platform/mcp-services', {
    params: {
      search: params.search || undefined,
      space_id: params.space_id || undefined,
      status: params.status || undefined,
    },
  });
}

/** 发布领域服务（标记为 MCP 可见） */
export async function publishService(serviceId: string): Promise<McpServiceItem> {
  return apiClient.post<McpServiceItem>(`/platform/mcp-services/${serviceId}/publish`);
}

/** 取消发布领域服务 */
export async function unpublishService(serviceId: string): Promise<McpServiceItem> {
  return apiClient.post<McpServiceItem>(`/platform/mcp-services/${serviceId}/unpublish`);
}

/** 测试调用已发布的服务（一期返回模拟回答） */
export async function testCallService(serviceId: string, query: string): Promise<TestCallResponse> {
  return apiClient.post<TestCallResponse>(`/platform/mcp-services/${serviceId}/test-call`, { query });
}

/** 查看已授权 MCP Key 列表 */
export async function getAuthorizedKeys(serviceId: string): Promise<AuthorizedKeyListResponse> {
  return apiClient.get<AuthorizedKeyListResponse>(`/platform/mcp-services/${serviceId}/authorized-keys`);
}

// ══ Tool 配置（DS-03：PUT /mcp-services/{service_id}/tool）══

export interface SaveToolConfigParams {
  /** Tool 名称：仅支持字母、数字、下划线（^[A-Za-z0-9_]+$），≤128 字符，同租户内唯一 */
  tool: string;
  /** Tool 描述 */
  tool_description?: string | null;
}

export interface SaveToolConfigResponse {
  service_id: string;
  tool: string;
  tool_description?: string | null;
}

/** 保存领域服务的 MCP Tool 配置（tool 名同租户内唯一，冲突时后端返回 code 1003） */
export async function saveToolConfig(
  serviceId: string,
  params: SaveToolConfigParams,
): Promise<SaveToolConfigResponse> {
  return apiClient.put<SaveToolConfigResponse>(`/platform/mcp-services/${serviceId}/tool`, {
    tool: params.tool,
    tool_description: params.tool_description || undefined,
  });
}
