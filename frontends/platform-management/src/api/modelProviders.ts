import apiClient from './client';

interface ApiEnvelope<T> {
  success: boolean;
  code?: number;
  message?: string;
  data?: T;
}

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

function unwrapEnvelope<T>(payload: ApiEnvelope<T>): T {
  if (!payload?.success) {
    throw new Error(payload?.message || 'Request failed');
  }
  return payload.data as T;
}

export async function listProviders(offset = 0, limit = 100): Promise<ModelProviderListResponse> {
  const resp = await apiClient.get<ApiEnvelope<ModelProviderListResponse>>('/api/v1/ecosystem/model-providers', {
    params: { offset, limit },
  });
  return unwrapEnvelope(resp.data);
}

export async function createProvider(data: SaveProviderPayload): Promise<ModelProviderItem> {
  const resp = await apiClient.post<ApiEnvelope<ModelProviderItem>>('/api/v1/ecosystem/model-providers', data);
  return unwrapEnvelope(resp.data);
}

export async function updateProvider(id: string, data: Partial<SaveProviderPayload>): Promise<ModelProviderItem> {
  const resp = await apiClient.patch<ApiEnvelope<ModelProviderItem>>(`/api/v1/ecosystem/model-providers/${id}`, data);
  return unwrapEnvelope(resp.data);
}

export async function testProvider(id: string): Promise<{ success: boolean; message: string }> {
  const resp = await apiClient.post<ApiEnvelope<{ success: boolean; message: string }>>(
    `/api/v1/ecosystem/model-providers/${id}/test`,
  );
  return unwrapEnvelope(resp.data);
}
