import apiClient from './request';

export interface TemplateDomain {
  id: string;
  name: string;
  name_en?: string;
  description?: string;
  status: 'active' | 'inactive' | 'archived';
  scenario_count?: number;
  created_at: string;
  updated_at?: string;
}

export async function fetchDomains(
  offset = 0,
  limit = 20,
): Promise<{ items: TemplateDomain[]; total: number; offset: number; limit: number }> {
  return apiClient.get<{ items: TemplateDomain[]; total: number; offset: number; limit: number }>(
    '/ecosystem/templates/domains',
    { params: { offset, limit } },
  );
}

export async function createDomain(data: {
  name: string;
  name_en?: string;
  description?: string;
  status: string;
}): Promise<TemplateDomain> {
  return apiClient.post<TemplateDomain>('/ecosystem/templates/domains', data);
}

export async function updateDomain(
  domainId: string,
  data: { name?: string; name_en?: string; description?: string; status?: string },
): Promise<TemplateDomain> {
  return apiClient.patch<TemplateDomain>(`/ecosystem/templates/domains/${domainId}`, data);
}

export async function deleteDomain(domainId: string): Promise<void> {
  await apiClient.delete<null>(`/ecosystem/templates/domains/${domainId}`);
}
