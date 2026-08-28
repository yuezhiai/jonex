import apiClient from './request';

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

export async function listDataAccessMethods(offset = 0, limit = 100): Promise<DataAccessListResponse> {
  return apiClient.get<DataAccessListResponse>('/ecosystem/data-access-methods', {
    params: { offset, limit },
  });
}
