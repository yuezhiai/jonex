import apiClient from './request';

export interface SkillListResponse {
  items: SkillItem[];
  total: number;
  offset?: number;
  limit?: number;
}

export interface SkillItem {
  id: string;
  name: string;
  description: string | null;
  category: string;
  icon?: string | null;
  status: 'published' | 'disabled';
  enabled: boolean;
  tenant_status: 'enabled' | 'disabled';
  tool_name: string;
  instruction: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  tags: string[];
  capability?: Record<string, unknown>;
}

export interface FetchSkillsParams {
  offset?: number;
  limit?: number;
  category?: string;
  keyword?: string;
}

export interface SkillToggleResponse {
  id: string;
  skill_id: string;
  tenant_status: 'enabled' | 'disabled';
  enabled: boolean;
}

export async function fetchSkills(params: FetchSkillsParams = {}): Promise<SkillListResponse> {
  return apiClient.get<SkillListResponse>('/ecosystem/skills', {
    params: {
      offset: params.offset ?? 0,
      limit: params.limit ?? 20,
      category: params.category,
      keyword: params.keyword,
    },
  });
}

export async function fetchSkill(skillId: string): Promise<SkillItem> {
  return apiClient.get<SkillItem>(`/ecosystem/skills/${skillId}`);
}

export async function enableSkill(skillId: string): Promise<SkillToggleResponse> {
  return apiClient.post<SkillToggleResponse>(`/ecosystem/skills/${skillId}/enable`);
}

export async function disableSkill(skillId: string): Promise<SkillToggleResponse> {
  return apiClient.post<SkillToggleResponse>(`/ecosystem/skills/${skillId}/disable`);
}

// ── 工具目录管理（平台管理员）────────────────────────────────────
// 平台共享目录：这里的新建 / 编辑 / 上下架对所有租户生效；租户侧只做启用 / 停用。

export type SkillCategory = 'image' | 'voice' | 'document' | 'video' | 'fusion' | 'custom';
export type CatalogStatus = 'published' | 'disabled';

/**
 * 工具运行时（格式与后端 jonex_core/common/tool_runtime.py 一致）：
 * echo 回显入参（联调用）；api 调用一个普通 HTTP 接口。null = 无可调入口。
 */
export type ToolRuntime =
  | { type: 'echo' }
  | {
      type: 'api';
      method: string;
      url: string;
      query?: Record<string, string>;
      headers?: Record<string, string>;
      body?: unknown;
      auth?: { type: string; in?: string; name?: string; username?: string };
      response?: { extract?: Record<string, string> };
      timeout?: number;
    };

export interface CatalogItem {
  id: string;
  name: string;
  description: string | null;
  category: SkillCategory | string;
  icon?: string | null;
  status: CatalogStatus;
  tool_name: string;
  instruction: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  tags: string[];
  runtime: ToolRuntime | null;
  /** 已保存鉴权凭据（凭据本身任何接口都不回传） */
  has_secret: boolean;
  /** 只读声明（显式勾选）；只有只读工具才可能被知识库问答自动调用 */
  read_only?: boolean;
  capability: Record<string, unknown>;
  has_artifact: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CatalogListResponse {
  items: CatalogItem[];
  total: number;
  offset: number;
  limit: number;
}

export interface FetchCatalogParams {
  offset?: number;
  limit?: number;
  status?: CatalogStatus;
  keyword?: string;
}

export interface SaveCatalogPayload {
  tool_name?: string;
  name?: string;
  description?: string | null;
  instruction?: string;
  category?: string;
  status?: CatalogStatus;
  tags?: string[];
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  runtime?: Record<string, unknown> | null;
  /** 新凭据；不传 / 空串表示不修改 */
  secret?: string;
  clear_secret?: boolean;
  /** 只读声明（显式勾选，默认 false = 写操作） */
  read_only?: boolean;
}

export async function fetchCatalog(params: FetchCatalogParams = {}): Promise<CatalogListResponse> {
  return apiClient.get<CatalogListResponse>('/ecosystem/skill-catalog', {
    params: {
      offset: params.offset ?? 0,
      limit: params.limit ?? 20,
      status: params.status,
      keyword: params.keyword || undefined,
    },
  });
}

export async function createCatalogTool(data: SaveCatalogPayload): Promise<CatalogItem> {
  return apiClient.post<CatalogItem>('/ecosystem/skill-catalog', data);
}

export async function updateCatalogTool(skillId: string, data: SaveCatalogPayload): Promise<CatalogItem> {
  return apiClient.patch<CatalogItem>(`/ecosystem/skill-catalog/${skillId}`, data);
}
