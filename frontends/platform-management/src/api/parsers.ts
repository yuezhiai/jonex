import apiClient from './client';

interface ApiEnvelope<T> {
  success: boolean;
  code?: number;
  message?: string;
  data?: T;
}

export interface ParserListResponse {
  items: ParserItem[];
  total: number;
  offset?: number;
  limit?: number;
}

export interface ParserItem {
  id: string;
  name: string;
  parser_type: string;
  file_types: string[];
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

export async function listParsers(offset = 0, limit = 100): Promise<ParserListResponse> {
  const resp = await apiClient.get<ApiEnvelope<ParserListResponse>>('/api/v1/ecosystem/parser-configs', {
    params: { offset, limit },
  });
  return unwrapEnvelope(resp.data);
}
