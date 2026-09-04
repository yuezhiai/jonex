import apiClient from './request';

// ── Types ──────────────────────────────────────────────────

export interface VersionItem {
  version: string;
  content: string;
  updated_by?: string;
  updated_at?: string;
  remark?: string;
}

export interface PromptTemplateItem {
  id: string;
  tenant_id: string | null; // null = system template
  name: string;
  category: string;
  scope: 'system' | 'domain';
  description?: string;
  status: string;
  current_version: string;
  versions_json: VersionItem[];
  created_by?: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface PromptTemplateDetail extends PromptTemplateItem {
  versions: VersionItem[];
  current_content?: string;
}

export interface PromptTemplateListResponse {
  items: PromptTemplateItem[];
  total: number;
  offset: number;
  limit: number;
}

export interface VersionListResponse {
  items: VersionItem[];
  current_version: string;
}

export interface CreatePromptTemplatePayload {
  name: string;
  category: string;
  description?: string;
  content: string;
  status?: string;
  domain_space_id?: string;
}

export interface UpdatePromptTemplatePayload {
  name?: string;
  category?: string;
  description?: string;
  content?: string;
  status?: string;
  version_remark?: string;
}

// ── Constants ──────────────────────────────────────────────

export const PROMPT_CATEGORIES = ['通用问答', '文档处理', '金融分析', '合同审查', '数据分析', '其他'] as const;

/** PROMPT_CATEGORIES 中文值 → i18n key 映射 */
export const PROMPT_CATEGORY_LABEL_KEYS: Record<string, string> = {
  通用问答: 'promptCategory.generalQA',
  文档处理: 'promptCategory.documentProcessing',
  金融分析: 'promptCategory.financialAnalysis',
  合同审查: 'promptCategory.contractReview',
  数据分析: 'promptCategory.dataAnalysis',
  其他: 'promptCategory.other',
};

export const CATEGORY_ICON_MAP: Record<string, { icon: string; bg: string }> = {
  通用问答: { icon: '💬', bg: 'linear-gradient(135deg, #3b82f6, #1d4ed8)' },
  文档处理: { icon: '📄', bg: 'linear-gradient(135deg, #f97316, #ea580c)' },
  金融分析: { icon: '📈', bg: 'linear-gradient(135deg, #10b981, #059669)' },
  合同审查: { icon: '⚖️', bg: 'linear-gradient(135deg, #8b5cf6, #7c3aed)' },
  数据分析: { icon: '🗄️', bg: 'linear-gradient(135deg, #06b6d4, #0891b2)' },
  其他: { icon: '📋', bg: 'linear-gradient(135deg, #64748b, #475569)' },
};

// ── API Functions（apiClient.get<T> 已解包，返回 data）──

export async function listPromptTemplates(params: {
  scope?: string;
  category?: string;
  keyword?: string;
  domain_space_id?: string;
  offset?: number;
  limit?: number;
}): Promise<PromptTemplateListResponse> {
  return apiClient.get<PromptTemplateListResponse>('/ecosystem/prompt-templates', { params });
}

export async function getPromptTemplate(id: string, domain_space_id?: string): Promise<PromptTemplateDetail> {
  const params: Record<string, string> = {};
  if (domain_space_id) params.domain_space_id = domain_space_id;
  return apiClient.get<PromptTemplateDetail>(`/ecosystem/prompt-templates/${id}`, { params });
}

export async function createPromptTemplate(
  data: CreatePromptTemplatePayload,
  domain_space_id?: string,
): Promise<PromptTemplateItem> {
  const payload = domain_space_id ? { ...data, domain_space_id } : data;
  return apiClient.post<PromptTemplateItem>('/ecosystem/prompt-templates', payload);
}

export async function updatePromptTemplate(
  id: string,
  data: UpdatePromptTemplatePayload,
  domain_space_id?: string,
): Promise<PromptTemplateItem> {
  const payload: Record<string, unknown> = { ...data };
  if (domain_space_id) payload.domain_space_id = domain_space_id;
  return apiClient.patch<PromptTemplateItem>(`/ecosystem/prompt-templates/${id}`, payload);
}

export async function deletePromptTemplate(id: string, domain_space_id?: string): Promise<void> {
  const params: Record<string, string> = {};
  if (domain_space_id) params.domain_space_id = domain_space_id;
  await apiClient.delete<null>(`/ecosystem/prompt-templates/${id}`, undefined, { params });
}

export async function copyPromptTemplate(id: string, domain_space_id?: string): Promise<PromptTemplateItem> {
  const payload = domain_space_id ? { domain_space_id } : undefined;
  return apiClient.post<PromptTemplateItem>(`/ecosystem/prompt-templates/${id}/copy`, payload);
}

export interface CheckPromptTemplateNameParams {
  name: string;
  domain_space_id?: string;
  template_id?: string;
}

export async function checkPromptTemplateName(
  params: CheckPromptTemplateNameParams,
): Promise<{ exists: boolean }> {
  return apiClient.get<{ exists: boolean }>('/ecosystem/prompt-templates/check-name', { params });
}


export async function listVersions(id: string, domain_space_id?: string): Promise<VersionListResponse> {
  const params: Record<string, string> = {};
  if (domain_space_id) params.domain_space_id = domain_space_id;
  return apiClient.get<VersionListResponse>(`/ecosystem/prompt-templates/${id}/versions`, { params });
}

export async function rollbackVersion(
  id: string,
  targetVersion: string,
  domain_space_id?: string,
): Promise<PromptTemplateItem> {
  const payload: Record<string, unknown> = { target_version: targetVersion };
  if (domain_space_id) payload.domain_space_id = domain_space_id;
  return apiClient.post<PromptTemplateItem>(`/ecosystem/prompt-templates/${id}/versions/rollback`, payload);
}
