import apiClient from './request';

export interface TenantListResponse {
  items: TenantItem[];
  total: number;
}

export interface TenantItem {
  id: string;
  name: string;
  description: string | null;
  status: number;
  plan_type: string;
  expire_time: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface TenantCreatePayload {
  id: string;
  name: string;
  description?: string;
  plan_type?: string;
}

export interface TenantUpdatePayload {
  name?: string;
  description?: string;
  plan_type?: string;
  status?: number;
}

export async function listTenants(page = 1, pageSize = 100): Promise<TenantListResponse> {
  return apiClient.get<TenantListResponse>('/platform/tenants', {
    params: { page, page_size: pageSize },
  });
}

export async function createTenant(data: TenantCreatePayload): Promise<TenantItem> {
  return apiClient.post<TenantItem>('/platform/tenants', data);
}

export async function updateTenant(id: string, data: TenantUpdatePayload): Promise<TenantItem> {
  return apiClient.patch<TenantItem>(`/platform/tenants/${id}`, data);
}

export async function deleteTenant(id: string): Promise<void> {
  await apiClient.delete<null>(`/platform/tenants/${id}`);
}

export async function getTenantUserCounts(): Promise<Record<string, number>> {
  return apiClient.get<Record<string, number>>('/platform/tenants/user-counts');
}

export function getPlanTypeLabel(t: (key: string) => string, plan: string): string {
  const labels: Record<string, string> = {
    free: t('tenants.planFree'),
    pro: t('tenants.planPro'),
    enterprise: t('tenants.planEnterprise'),
  };
  return labels[plan] || plan;
}

export function getStatusLabel(t: (key: string) => string, status: number): { label: string; color: string } {
  return status === 1
    ? { label: t('status.enabled'), color: 'success' }
    : { label: t('status.disabled'), color: 'warning' };
}
