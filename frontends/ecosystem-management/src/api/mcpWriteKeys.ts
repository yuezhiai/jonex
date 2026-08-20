import apiClient from './client';

// ══ 知识写入 Key 类型（来源 /api/v1/platform/mcp-write-keys，契约见 Phase 17 移交文档）══

/** 单个知识库写入授权（grants[] 列表项） */
export interface WriteGrant {
  /** 知识库 ID（uuid） */
  kb: string;
  /** "all"=全目录可写（directories 必须为空）| "specified"=仅指定目录可写（directories 必须非空） */
  mode: 'all' | 'specified';
  /** 平铺 folder_id 列表；仅 mode=specified 时非空 */
  directories: string[];
}

export interface WriteKeyCreatePayload {
  /** 必填，max 255，strip 后非空 */
  name: string;
  /** 有效期（ISO datetime；null = 永久有效） */
  expires_at?: string | null;
  /** 写入范围，必填且不能为空数组 */
  grants: WriteGrant[];
}

/** 编辑请求（PUT 局部更新，全 Optional；null = 不改动该字段） */
export interface WriteKeyUpdatePayload {
  name?: string | null;
  expires_at?: string | null;
  grants?: WriteGrant[] | null;
}

/** 列表 / 详情 / 编辑 / toggle 响应（脱敏，不含明文） */
export interface WriteKeyItem {
  id: string;
  name: string;
  /** 脱敏前缀：明文去掉 mcpw_ 后的前 8 位（不含前缀本身） */
  key_prefix: string;
  /** 4 态派生：active / disabled / expired / revoked（revoked > expired > disabled > active） */
  status: string;
  grants: WriteGrant[];
  /** 后端由 grants[0].kb 反推，前端无需传 */
  space_id: string | null;
  /** 冗余 = grants[0].kb */
  kb_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  expires_at: string | null;
  disabled_at: string | null;
  revoked_at: string | null;
}

/** 创建响应——一次性明文仅在此返回一次（HTTP 201） */
export interface WriteKeyCreateResult {
  id: string;
  plaintext: string;
  name: string;
  key_prefix: string;
  grants: WriteGrant[];
  space_id: string | null;
  kb_id: string | null;
  created_at: string;
  expires_at: string | null;
}

export interface WriteKeyListResponse {
  items: WriteKeyItem[];
  total: number;
}

/** 明文真实前缀（区别于服务访问 Key 的 yxm_） */
export const WRITE_KEY_PREFIX = 'mcpw_';

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

// ══ 接口方法（6 端点，前缀 /api/v1/platform/mcp-write-keys）══

/** 全量列出当前租户下所有知识写入 Key（含已撤销，脱敏） */
export async function listMcpWriteKeys(): Promise<WriteKeyListResponse> {
  const resp = await apiClient.get<ApiEnvelope<WriteKeyListResponse>>('/api/v1/platform/mcp-write-keys');
  return unwrap(resp.data);
}

/** 创建知识写入 Key，返回值含一次性 plaintext（mcpw_ 前缀） */
export async function createMcpWriteKey(data: WriteKeyCreatePayload): Promise<WriteKeyCreateResult> {
  const resp = await apiClient.post<ApiEnvelope<WriteKeyCreateResult>>('/api/v1/platform/mcp-write-keys', data);
  return unwrap(resp.data);
}

/** 获取单个知识写入 Key 详情（脱敏） */
export async function getMcpWriteKeyDetail(keyId: string): Promise<WriteKeyItem> {
  const resp = await apiClient.get<ApiEnvelope<WriteKeyItem>>(`/api/v1/platform/mcp-write-keys/${keyId}`);
  return unwrap(resp.data);
}

/** 编辑知识写入 Key（name / expires_at / grants 全 Optional） */
export async function updateMcpWriteKey(keyId: string, data: WriteKeyUpdatePayload): Promise<WriteKeyItem> {
  const resp = await apiClient.put<ApiEnvelope<WriteKeyItem>>(`/api/v1/platform/mcp-write-keys/${keyId}`, data);
  return unwrap(resp.data);
}

/** 停用/启用知识写入 Key（可逆，key 不变；已撤销 Key 调 toggle → 409） */
export async function toggleMcpWriteKey(keyId: string): Promise<WriteKeyItem> {
  const resp = await apiClient.post<ApiEnvelope<WriteKeyItem>>(`/api/v1/platform/mcp-write-keys/${keyId}/toggle`);
  return unwrap(resp.data);
}

/** 撤销知识写入 Key（不可逆，设置 revoked_at） */
export async function revokeMcpWriteKey(keyId: string): Promise<void> {
  await apiClient.post<ApiEnvelope<void>>(`/api/v1/platform/mcp-write-keys/${keyId}/revoke`);
}

/** 系统内置写工具 Tool 名（hero 卡展示，固定不可改） */
export const WRITE_TOOL_NAME = 'knowledge_document_write';
