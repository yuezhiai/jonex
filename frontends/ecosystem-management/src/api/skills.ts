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
