import apiClient from './request';

export interface ModelProviderListResponse {
  items: ModelProviderItem[];
  total: number;
  offset?: number;
  limit?: number;
}

export interface ModelProviderItem {
  id: string;
  name: string;
  provider_type: string;
  model_type: string | null;
  endpoint: string | null;
  model_name: string | null;
  vector_dimension: number | null;
  token_limit: number | null;
  latency_ms: number | null;
  call_count: number | null;
  success_rate: number | null;
  status: string;
  config_json: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface SaveProviderPayload {
  name: string;
  provider_type: string;
  model_type?: string;
  endpoint?: string;
  api_key?: string;
  model_name?: string;
  config_json?: Record<string, unknown>;
}

export async function listProviders(offset = 0, limit = 100): Promise<ModelProviderListResponse> {
  return apiClient.get<ModelProviderListResponse>('/ecosystem/model-providers', {
    params: { offset, limit },
  });
}

export async function createProvider(data: SaveProviderPayload): Promise<ModelProviderItem> {
  return apiClient.post<ModelProviderItem>('/ecosystem/model-providers', data);
}

export async function updateProvider(id: string, data: Partial<SaveProviderPayload>): Promise<ModelProviderItem> {
  return apiClient.patch<ModelProviderItem>(`/ecosystem/model-providers/${id}`, data);
}

export async function testProvider(id: string): Promise<{ success: boolean; message: string }> {
  return apiClient.post<{ success: boolean; message: string }>(`/ecosystem/model-providers/${id}/test`);
}
