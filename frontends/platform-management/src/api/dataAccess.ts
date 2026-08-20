import apiClient from './client';

interface ApiEnvelope<T> {
  success: boolean;
  code?: number;
  message?: string;
  data?: T;
}

export interface DataAccessListResponse {
  items: DataAccessItem[];
  total: number;
  offset?: number;
  limit?: number;
}

export interface DataAccessItem {
  id: string;
  name: string;
  access_type: string;
  description?: string | null;
  config_json: Record<string, unknown>;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

function unwrapEnvelope<T>(payload: ApiEnvelope<T>): T {
  if (!payload?.success) {
    throw new Error(payload?.message || 'Request failed');
  }
  return payload.data as T;
}

export async function listDataAccessMethods(offset = 0, limit = 100): Promise<DataAccessListResponse> {
  const resp = await apiClient.get<ApiEnvelope<DataAccessListResponse>>('/api/v1/ecosystem/data-access-methods', {
    params: { offset, limit },
  });
  return unwrapEnvelope(resp.data);
}
