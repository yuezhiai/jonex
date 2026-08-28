import apiClient from './request';

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

export async function listParsers(offset = 0, limit = 100): Promise<ParserListResponse> {
  return apiClient.get<ParserListResponse>('/ecosystem/parser-configs', {
    params: { offset, limit },
  });
}
